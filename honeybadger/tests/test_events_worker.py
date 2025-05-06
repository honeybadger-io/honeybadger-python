import time
from types import SimpleNamespace
import pytest
from honeybadger.events_worker import EventsWorker, EventsSendResult, Event
from honeybadger.types import EventsSendStatus


class DummyConnection:
    """Stub with configurable behavior for send_events."""

    def __init__(self, behaviors=None):
        self.behaviors = behaviors or []
        self.call_count = 0
        self.batches = []

    def send_events(self, cfg, batch: Event) -> EventsSendResult:
        self.batches.append(batch)
        if self.call_count < len(self.behaviors):
            result = self.behaviors[self.call_count]
        else:
            result = EventsSendResult(EventsSendStatus.OK)
        self.call_count += 1
        return result


@pytest.fixture
def base_config():
    return SimpleNamespace(
        api_key="key",
        endpoint="url",
        environment="env",
        events_batch_size=3,
        events_max_queue_size=10,
        events_timeout=0.1,
        events_max_batch_retries=2,
        events_throttle_wait=0.1,
    )


@pytest.fixture
def worker(base_config):
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=base_config)
    yield w, conn
    w.shutdown()


def wait_for(predicate, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_batch_send_on_batch_size(worker):
    w, conn = worker
    events = [{"id": i} for i in (1, 2, 3)]
    for e in events:
        assert w.push(e)
    time.sleep(0.05)
    assert conn.batches == [events]


def test_no_send_under_batch_size(worker):
    w, conn = worker
    for e in ({"id": 1}, {"id": 2}):
        assert w.push(e)
    time.sleep(0.05)
    assert conn.batches == []


def test_drop_events_when_queue_full(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_max_queue_size = 4
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)
    dropped = 0
    for i in range(6):
        if not w.push({"id": i + 1}):
            dropped += 1
    stats = w.get_stats()
    assert dropped == 2
    assert stats["dropped_events"] == 2
    assert stats["queue_size"] == 4
    w.shutdown()


def test_flush_on_timeout(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_batch_size = 10
    cfg.events_timeout = 0.05
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)
    for e in ({"id": 1}, {"id": 2}):
        w.push(e)
    time.sleep(cfg.events_timeout + 0.05)
    assert conn.batches == [[{"id": 1}, {"id": 2}]]
    w.shutdown()


def test_reset_timer_after_send(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_timeout = 0.05
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)
    first = [{"id": 1}, {"id": 2}, {"id": 3}]
    for e in first:
        w.push(e)
    assert wait_for(lambda: len(conn.batches) >= 1, cfg.events_timeout + 0.02)
    assert conn.batches[0] == first
    second = [{"id": 4}, {"id": 5}]
    for e in second:
        w.push(e)
    time.sleep(cfg.events_timeout / 2)
    assert len(conn.batches) == 1
    time.sleep(cfg.events_timeout)
    assert conn.batches[1] == second
    w.shutdown()


def test_retry_and_drop_after_max_retries(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_batch_size = 2
    cfg.events_timeout = 0.05
    cfg.events_max_batch_retries = 3
    behaviors = [
        EventsSendResult(EventsSendStatus.ERROR, "fail")
    ] * cfg.events_max_batch_retries
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)
    for e in ({"id": 1}, {"id": 2}):
        w.push(e)
    time.sleep(cfg.events_timeout * (cfg.events_max_batch_retries + 1))
    assert len(conn.batches) == cfg.events_max_batch_retries
    assert w.get_stats()["batch_count"] == 0
    w.shutdown()


def test_queue_new_events_during_retries(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_batch_size = 2
    cfg.events_timeout = 0.05
    cfg.events_max_batch_retries = 2
    behaviors = [
        EventsSendResult(EventsSendStatus.ERROR, "fail"),
        EventsSendResult(EventsSendStatus.OK),
    ]
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)
    first = [{"id": 1}, {"id": 2}]
    for e in first:
        w.push(e)
    time.sleep(0.01)
    second = [{"id": 3}, {"id": 4}]
    for e in second:
        w.push(e)
    time.sleep(cfg.events_timeout * 2)
    assert conn.batches[0] == first
    assert conn.batches[1] == first
    assert conn.batches[2] == second
    w.shutdown()


def test_does_not_reset_timer_on_subsequent_pushes(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_batch_size = 100
    cfg.events_timeout = 0.1

    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)

    w.push({"id": 1})
    time.sleep(cfg.events_timeout * 0.4)
    w.push({"id": 2})
    time.sleep(cfg.events_timeout * 0.4)
    w.push({"id": 3})

    assert wait_for(lambda: len(conn.batches) >= 1, cfg.events_timeout * 1.1)
    assert conn.batches[0] == [{"id": 1}, {"id": 2}, {"id": 3}]

    w.shutdown()


def test_pushes_after_flush(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_batch_size = 100
    cfg.events_timeout = 0.05
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)
    w.push({"id": 1})
    time.sleep(0.06)
    assert conn.batches[0] == [{"id": 1}]
    w.push({"id": 2})
    assert len(conn.batches) == 1
    time.sleep(cfg.events_timeout + 0.01)
    assert conn.batches[1] == [{"id": 2}]
    w.shutdown()


def test_throttling_and_resume(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_batch_size = 2
    cfg.events_timeout = 0.05
    behaviors = [
        EventsSendResult(EventsSendStatus.ERROR, "throttled"),
        EventsSendResult(EventsSendStatus.OK),
        EventsSendResult(EventsSendStatus.OK),
    ]
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)
    first = [{"id": 1}, {"id": 2}]
    for e in first:
        w.push(e)
    time.sleep(0.01)
    second = [{"id": 3}, {"id": 4}]
    for e in second:
        w.push(e)
    time.sleep(cfg.events_throttle_wait + cfg.events_timeout * 2)
    assert conn.batches[0] == first
    assert conn.batches[1] == first
    assert conn.batches[2] == second
    w.shutdown()


def test_true_throttling_status_flips_throttled_flag_and_retries_fast(base_config):
    cfg = base_config
    cfg.events_timeout = 0.01
    cfg.events_throttle_wait = 0.01

    behaviors = [
        EventsSendResult(EventsSendStatus.THROTTLING),
        EventsSendResult(EventsSendStatus.OK),
    ]
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)

    for i in range(cfg.events_batch_size):
        assert w.push({"id": i})

    assert wait_for(
        lambda: conn.call_count >= 1, timeout=0.05
    ), f"first send never happened, call_count={conn.call_count}"
    assert w.get_stats()["throttling"] is True

    assert wait_for(
        lambda: conn.call_count >= 2, timeout=0.1
    ), f"retry never happened, call_count={conn.call_count}"
    assert w.get_stats()["throttling"] is False

    w.shutdown()


def test_flush_delay_respects_throttle_wait(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_batch_size = 2
    cfg.events_timeout = 0.05
    cfg.events_throttle_wait = 0.15

    behaviors = [
        EventsSendResult(EventsSendStatus.ERROR, "throttled"),
        EventsSendResult(EventsSendStatus.OK),
    ]
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)

    for e in ({"id": 1}, {"id": 2}):
        w.push(e)

    assert wait_for(lambda: len(conn.batches) >= 1, 0.1)
    assert wait_for(lambda: len(conn.batches) >= 2, cfg.events_throttle_wait + 0.1)
    assert conn.batches[1] == [{"id": 1}, {"id": 2}]
    w.shutdown()


def test_interleave_new_events_during_throttle_backoff(base_config):
    cfg = base_config
    behaviors = [
        EventsSendResult(EventsSendStatus.THROTTLING),
        EventsSendResult(EventsSendStatus.OK),
    ]
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)

    first = [{"id": 1}, {"id": 2}, {"id": 3}]
    for e in first:
        w.push(e)

    assert wait_for(
        lambda: len(conn.batches) >= 1, timeout=cfg.events_timeout
    ), f"Expected first batch within {cfg.events_timeout}s"

    second = [{"id": 4}, {"id": 5}, {"id": 6}]
    for e in second:
        w.push(e)

    total_wait = cfg.events_throttle_wait + cfg.events_timeout * 2
    assert wait_for(
        lambda: len(conn.batches) >= 3, timeout=total_wait
    ), f"Expected 3 batches within {total_wait}s"

    assert conn.batches[0] == first
    assert conn.batches[1] == first
    assert conn.batches[2] == second

    w.shutdown()


def test_send_remaining_on_shutdown(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.events_batch_size = 100
    cfg.events_timeout = 1.0
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)
    for e in ({"id": 1}, {"id": 2}):
        w.push(e)
    w.shutdown()
    assert conn.batches[-1] == [{"id": 1}, {"id": 2}]
