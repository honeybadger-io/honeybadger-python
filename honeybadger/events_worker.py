import time
import threading
import logging
from collections import deque
from typing import Deque, Dict, Any, Optional, Tuple, Union, List

from .protocols import Connection
from .config import Configuration
from .types import EventsSendStatus, EventsSendResult, Event


class EventsWorker:
    """
    Batches events and sends with retry and backoff.
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
        self._cond = threading.Condition(self._lock)
        self._queue: Deque[Event] = deque()
        self._batches: Deque[Tuple[List[Event], int]] = deque()
        self._throttled = False
        self._stop = False
        self._dropped = 0
        self._last_drop_log = time.monotonic()
        self._start_time: Optional[float] = None

        self._thread = threading.Thread(
            target=self._run,
            name="honeybadger-events-worker",
            daemon=True,
        )
        self._thread.start()
        self.log.debug("Events worker started")

    def push(self, event: Event) -> bool:
        with self._cond:
            if self._all_events_queued_len() >= self.config.insights_max_queue:
                self._drop()
                return False

            self._queue.append(event)
            if len(self._queue) >= self.config.insights_batch_size:
                self._cond.notify()

        return True

    def flush(self) -> None:
        with self._cond:
            self._cond.notify()

    def shutdown(self) -> None:
        self.log.debug("Shutting down events worker")
        with self._cond:
            self._stop = True
            self._cond.notify()

        if self._thread.is_alive():
            timeout = max(self.config.insights_flush_interval, self.config.insights_throttle_backoff) * 2
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
        while True:
            with self._cond:
                woke = self._cond.wait_for(
                    lambda: self._stop or len(self._queue) >= self.config.insights_batch_size,
                    timeout=self.config.insights_flush_interval,
                )
                # if stopped and truly empty, we’re done
                if self._stop and not self._queue and not self._batches:
                    break

            self._flush()

    def _flush(self) -> None:
        with self._lock:
            if self._queue:
                batch = list(self._queue)
                self._queue.clear()
                self._batches.append((batch, 0))
                self._start_time = None

            new: Deque[Tuple[List[Event], int]] = deque()
            throttled = False

            while self._batches:
                batch, attempts = self._batches.popleft()
                if throttled:
                    new.append((batch, attempts))
                    continue

                result = self._safe_send(batch)
                if result.status == EventsSendStatus.OK:
                    continue

                attempts += 1
                if result.status == EventsSendStatus.THROTTLING:
                    throttled = True
                    self.log.warning(
                        f"Rate limited – backing off {self.config.insights_throttle_backoff}s"
                    )
                else:
                    reason = result.reason or "unknown"
                    self.log.debug(f"Batch failed (attempt {attempts}): {reason}")

                if attempts < self.config.insights_max_retries:
                    new.append((batch, attempts))
                else:
                    self.log.debug(f"Dropping batch after {attempts} retries")

            self._batches = new
            self._throttled = throttled

    def _safe_send(self, batch: List[Event]) -> EventsSendResult:
        try:
            return self.connection.send_events(self.config, batch)
        except Exception:
            self.log.exception("Exception sending batch")
            return EventsSendResult(EventsSendStatus.ERROR, "exception")

    def _compute_timeout(self) -> Optional[float]:
        if self._throttled:
            return self.config.insights_throttle_backoff
        if not self._start_time:
            return None
        elapsed = time.monotonic() - self._start_time
        return max(0, self.config.insights_flush_interval - elapsed)

    def _drop(self) -> None:
        self._dropped += 1
        now = time.monotonic()
        if now - self._last_drop_log >= self._DROP_LOG_INTERVAL:
            self.log.info(f"Dropped {self._dropped} events (queue full)")
            self._dropped = 0
            self._last_drop_log = now

    def _all_events_queued_len(self) -> int:
        return len(self._queue) + sum(len(b) for b, _ in self._batches)

    @classmethod
    def create(
        cls,
        config: Configuration,
        connection: Connection,
        logger: Optional[logging.Logger] = None,
    ) -> "EventsWorker":
        return cls(connection=connection, config=config, logger=logger)
