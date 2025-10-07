import os
import time
import threading
import logging
from collections import deque
from typing import Deque, Dict, Any, Optional, Tuple, List

from .protocols import Connection
from .config import Configuration
from .types import EventsSendStatus, EventsSendResult, Event


class EventsWorker:
    """
    Asynchronously batches events and sends them to a backend connection,
    applying retry logic, rate-limit backoff, and drop-on-overflow.
    """

    _DROP_LOG_INTERVAL = 60.0  # seconds

    def __init__(
        self,
        connection: Connection,
        config: Configuration,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.connection = connection
        self.config = config

        self.log = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._batch_ready_event = threading.Event()

        self._queue: Deque[Event] = deque()
        self._batches: Deque[Tuple[List[Event], int]] = deque()

        self._throttled = False
        self._stop = False
        self._dropped = 0
        self._last_drop_log = time.monotonic()
        self._start_time = time.monotonic()

        self._thread = threading.Thread(
            target=self._run,
            name=f"honeybadger-events-worker-{os.getpid()}",
            daemon=True,
        )
        self._thread.start()
        self.log.debug("Events worker started")

    def restart(self):
        """Restart the batch worker thread (useful after process forking)"""
        if hasattr(self, "_thread") and self._thread and self._thread.is_alive():
            self.shutdown()

        # Reset state
        self._stop = False

        self._thread = threading.Thread(
            target=self._run,
            name=f"honeybadger-events-worker-{os.getpid()}",
            daemon=True,
        )
        self._thread.start()

        return self._thread.is_alive()

    def push(self, event: Event) -> bool:
        # Small race condition is acceptable - may slightly exceed max size briefly
        current_size = self._all_events_queued_len()
        if current_size >= self.config.events_max_queue_size:
            self._drop()
            return False

        self._queue.append(event)

        # Signal worker thread if batch size reached
        if len(self._queue) >= self.config.events_batch_size:
            self._batch_ready_event.set()

        return True

    def shutdown(self) -> None:
        self.log.debug("Shutting down events worker")
        self._stop = True
        self._batch_ready_event.set()  # Wake up the worker thread

        if self._thread.is_alive():
            timeout = (
                max(
                    self.config.events_timeout,
                    self.config.events_throttle_wait,
                )
                * 2
            )
            self._thread.join(timeout)
        self.log.debug("Events worker stopped")

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "queue_size": len(self._queue),
                "batch_count": len(self._batches),
                "total_events": self._all_events_queued_len(),
                "dropped_events": self._dropped,
                "throttling": self._throttled,
            }

    def _run(self) -> None:
        """
        Main loop: wait until stop or enough events to batch, then flush.
        """
        while True:
            # Wait for batch ready signal or timeout
            self._batch_ready_event.wait(timeout=self._compute_timeout())
            self._batch_ready_event.clear()

            # Check if we should exit (need consistent view of state)
            with self._lock:
                if self._stop and not self._queue and not self._batches:
                    break

            # Perform send/retry logic
            self._flush()

    def _flush(self) -> None:
        """
        Move queued events into a pending batch list, then attempt to send
        each batch with retry/backoff. Update throttled state and pending list.
        """
        with self._lock:
            # If there are new queued events, package them as a fresh batch
            # Use popleft() which is atomic/thread-safe (unlike list() or clear())
            batch = []
            while True:
                try:
                    batch.append(self._queue.popleft())
                except IndexError:
                    break

            if batch:
                self._batches.append((batch, 0))
                self._start_time = time.monotonic()

            new: Deque[Tuple[List[Event], int]] = deque()
            throttled = False

            # Process each batch in FIFO order
            while self._batches:
                batch, attempts = self._batches.popleft()
                # If already throttled earlier this pass, skip sends
                if throttled:
                    new.append((batch, attempts))
                    continue

                # Attempt to send; wrap in try/except for resiliency
                try:
                    result = self.connection.send_events(self.config, batch)
                except Exception as err:
                    self.log.exception("Unexpected error sending batch")
                    result = EventsSendResult(EventsSendStatus.ERROR, str(err))

                if result.status == EventsSendStatus.OK:
                    continue

                attempts += 1
                # Rate-limited path
                if result.status == EventsSendStatus.THROTTLING:
                    throttled = True
                    self.log.warning(
                        f"Rate limited – backing off {self.config.events_throttle_wait}s"
                    )
                else:
                    reason = result.reason or "unknown"
                    self.log.debug(f"Batch failed (attempt {attempts}): {reason}")

                # Retry or drop based on max_retries
                if attempts < self.config.events_max_batch_retries:
                    new.append((batch, attempts))
                else:
                    self.log.debug(f"Dropping batch after {attempts} retries")

            # Replace batch list and set throttling flag
            self._batches = new
            self._throttled = throttled

    def _compute_timeout(self) -> float:
        """
        Determine sleep time: use backoff if throttled, else fixed flush interval.
        """
        if self._throttled:
            return self.config.events_throttle_wait
        return self.config.events_timeout

    def _drop(self) -> None:
        """
        Increment drop counter and occasionally log a summary.
        """
        self._dropped += 1
        now = time.monotonic()
        if now - self._last_drop_log >= self._DROP_LOG_INTERVAL:
            self.log.info(f"Dropped {self._dropped} events (queue full)")
            self._dropped = 0
            self._last_drop_log = now

    def _all_events_queued_len(self) -> int:
        return len(self._queue) + sum(len(b) for b, _ in self._batches)
