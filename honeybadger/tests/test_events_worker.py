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
            result = EventsSendStatus.OK
        self.call_count += 1
        return result


@pytest.fixture
def base_config():
    return SimpleNamespace(
        api_key="key",
        endpoint="url",
        environment="env",
        insights_batch_size=3,
        insights_max_queue=10,
        insights_flush_interval=0.1,
        insights_max_retries=2,
        insights_throttle_backoff=0.1,
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
    cfg.insights_max_queue = 4
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
    cfg.insights_batch_size = 10
    cfg.insights_flush_interval = 0.05
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)
    for e in ({"id": 1}, {"id": 2}):
        w.push(e)
    time.sleep(cfg.insights_flush_interval + 0.05)
    assert conn.batches == [[{"id": 1}, {"id": 2}]]
    w.shutdown()


def test_reset_timer_after_send(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.insights_flush_interval = 0.05
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)
    first = [{"id": 1}, {"id": 2}, {"id": 3}]
    for e in first:
        w.push(e)
    assert wait_for(lambda: len(conn.batches) >= 1, cfg.insights_flush_interval + 0.02)
    assert conn.batches[0] == first
    second = [{"id": 4}, {"id": 5}]
    for e in second:
        w.push(e)
    time.sleep(cfg.insights_flush_interval / 2)
    assert len(conn.batches) == 1
    time.sleep(cfg.insights_flush_interval)
    assert conn.batches[1] == second
    w.shutdown()


def test_retry_and_drop_after_max_retries(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.insights_batch_size = 2
    cfg.insights_flush_interval = 0.05
    cfg.insights_max_retries = 3
    behaviors = [EventsSendResult(EventsSendStatus.ERROR, "fail")] * cfg.insights_max_retries
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)
    for e in ({"id": 1}, {"id": 2}):
        w.push(e)
    time.sleep(cfg.insights_flush_interval * (cfg.insights_max_retries + 1))
    assert len(conn.batches) == cfg.insights_max_retries
    assert w.get_stats()["batch_count"] == 0
    w.shutdown()


def test_queue_new_events_during_retries(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.insights_batch_size = 2
    cfg.insights_flush_interval = 0.05
    cfg.insights_max_retries = 2
    behaviors = [EventsSendResult(EventsSendStatus.ERROR, "fail"), EventsSendStatus.OK]
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)
    first = [{"id": 1}, {"id": 2}]
    for e in first:
        w.push(e)
    time.sleep(0.01)
    second = [{"id": 3}, {"id": 4}]
    for e in second:
        w.push(e)
    time.sleep(cfg.insights_flush_interval * 2)
    assert conn.batches[0] == first
    assert conn.batches[1] == first
    assert conn.batches[2] == second
    w.shutdown()


def test_does_not_reset_timer_on_subsequent_pushes(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.insights_batch_size = 100
    cfg.insights_flush_interval = 0.1

    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)

    w.push({"id": 1})
    time.sleep(cfg.insights_flush_interval * 0.4)
    w.push({"id": 2})
    time.sleep(cfg.insights_flush_interval * 0.4)
    w.push({"id": 3})

    assert wait_for(lambda: len(conn.batches) >= 1, cfg.insights_flush_interval * 1.1)
    assert conn.batches[0] == [{"id": 1}, {"id": 2}, {"id": 3}]

    w.shutdown()


def test_pushes_after_flush(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.insights_batch_size = 100
    cfg.insights_flush_interval = 0.05
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)
    w.push({"id": 1})
    time.sleep(0.06)
    assert conn.batches[0] == [{"id": 1}]
    w.push({"id": 2})
    assert len(conn.batches) == 1
    time.sleep(cfg.insights_flush_interval + 0.01)
    assert conn.batches[1] == [{"id": 2}]
    w.shutdown()


def test_throttling_and_resume(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.insights_batch_size = 2
    cfg.insights_flush_interval = 0.05
    cfg.insights_throttle_backoff = 0.1
    behaviors = [EventsSendResult(EventsSendStatus.ERROR, "throttled"), EventsSendStatus.OK, EventsSendStatus.OK]
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)
    first = [{"id": 1}, {"id": 2}]
    for e in first:
        w.push(e)
    time.sleep(0.01)
    second = [{"id": 3}, {"id": 4}]
    for e in second:
        w.push(e)
    time.sleep(cfg.insights_throttle_backoff + cfg.insights_flush_interval * 2)
    assert conn.batches[0] == first
    assert conn.batches[1] == first
    assert conn.batches[2] == second
    w.shutdown()


def test_flush_delay_respects_throttle_wait(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.insights_batch_size = 2
    cfg.insights_flush_interval = 0.05
    cfg.insights_throttle_backoff = 0.15

    behaviors = [EventsSendResult(EventsSendStatus.ERROR, "throttled"), EventsSendStatus.OK]
    conn = DummyConnection(behaviors=behaviors)
    w = EventsWorker(connection=conn, config=cfg)

    for e in ({"id": 1}, {"id": 2}):
        w.push(e)

    assert wait_for(lambda: len(conn.batches) >= 1, 0.1)
    assert wait_for(lambda: len(conn.batches) >= 2, cfg.insights_throttle_backoff + 0.1)
    assert conn.batches[1] == [{"id": 1}, {"id": 2}]
    w.shutdown()


def test_send_remaining_on_shutdown(base_config):
    cfg = SimpleNamespace(**vars(base_config))
    cfg.insights_batch_size = 100
    cfg.insights_flush_interval = 1.0
    conn = DummyConnection()
    w = EventsWorker(connection=conn, config=cfg)
    for e in ({"id": 1}, {"id": 2}):
        w.push(e)
    w.shutdown()
    assert conn.batches[-1] == [{"id": 1}, {"id": 2}]
