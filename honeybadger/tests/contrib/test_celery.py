import os
import time
import re
from unittest.mock import patch, MagicMock

import pytest

from celery import Celery
from honeybadger.contrib.celery import CeleryHoneybadger, CeleryPlugin
from honeybadger.tests.utils import with_config


@patch("honeybadger.honeybadger.reset_context")
@patch("honeybadger.honeybadger.notify")
def test_notify_from_task_failure(notify, reset_context):
    from celery.signals import task_failure, task_postrun

    app = Celery(__name__, broker="memory://")
    exception = Exception("Test exception")
    hb = CeleryHoneybadger(app, report_exceptions=True)

    # Send task_failure event
    task_failure.send(
        sender=app, task_id="hi", task_name="tasks.add", exception=exception
    )

    assert notify.call_count == 1
    assert notify.call_args[1]["exception"] == exception

    task_postrun.send(
        sender=app, task_id="hi", task_name="tasks.add", task={"name": "tasks.add"}
    )

    assert reset_context.call_count == 1

    hb.tearDown()


@patch("honeybadger.honeybadger.notify")
def test_notify_not_called_from_task_failure(mock):
    from celery.signals import task_failure

    app = Celery(__name__, broker="memory://")
    hb = CeleryHoneybadger(app, report_exceptions=False)

    # Send task_failure event
    task_failure.send(
        sender=app, task_name="tasks.add", exception=Exception("Test exception")
    )

    assert mock.call_count == 0
    hb.tearDown()


def test_plugin_payload():
    test_task = MagicMock()
    test_task.name = "test_task"
    test_task.max_retries = 10
    test_task.request = MagicMock(
        id="test_id",
        name="test_task",
        args=(1, 2),
        retries=0,
        max_retries=10,
        kwargs={"foo": "bar"},
    )

    with patch("celery.current_task", test_task):
        plugin = CeleryPlugin()
        payload = plugin.generate_payload({"request": {}}, {}, {})
        request = payload["request"]
        assert request["component"] == "unittest.mock"
        assert request["action"] == "test_task"
        assert request["params"]["args"] == [1, 2]
        assert request["params"]["kwargs"] == {"foo": "bar"}
        assert request["context"]["task_id"] == "test_id"
        assert request["context"]["retries"] == 0
        assert request["context"]["max_retries"] == 10


def setup_celery_hb(insights_enabled=True):
    from celery.signals import worker_ready

    app = Celery(__name__, broker="memory://localhost/")
    app.conf.update(
        HONEYBADGER_INSIGHTS_ENABLED=insights_enabled,
        HONEYBADGER_API_KEY="test_api_key",
    )
    hb = CeleryHoneybadger(app)
    worker_ready.send(sender=app)
    time.sleep(0.2)
    return app, hb


@patch("honeybadger.honeybadger.event")
def test_finished_task_event(mock_event):
    _, hb = setup_celery_hb()

    task = MagicMock()
    task.name = "test_task"
    task.request.retries = 0
    task.request.group = None
    task.request.args = []
    task.request.kwargs = {}

    hb._on_task_prerun("test_task_id", task)
    hb._on_task_postrun("test_task_id", task, state="SUCCESS")

    # Verify honeybadger.event was called directly
    assert mock_event.call_count == 1
    assert mock_event.call_args[0][0] == "celery.task_finished"

    payload = mock_event.call_args[0][1]
    assert payload["task_id"] == "test_task_id"
    assert payload["task_name"] == "test_task"
    assert payload["state"] == "SUCCESS"

    hb.tearDown()


@with_config({"insights_config": {"celery": {"include_args": True}}})
@patch("honeybadger.honeybadger.event")
def test_includes_task_args(mock_event):
    _, hb = setup_celery_hb()

    task = MagicMock()
    task.request.group = None
    task.name = "test_task"
    task.request.retries = 1
    task.request.args = [1, 2]
    task.request.kwargs = {"foo": "bar", "password": "secret"}

    hb._on_task_prerun("test_task_id", task)
    hb._on_task_postrun("test_task_id", task, state="SUCCESS")

    assert mock_event.call_count == 1
    assert mock_event.call_args[0][0] == "celery.task_finished"

    payload = mock_event.call_args[0][1]

    assert payload["task_id"] == "test_task_id"
    assert payload["task_name"] == "test_task"
    assert payload["args"] == [1, 2]
    assert payload["kwargs"] == {"foo": "bar"}  # password should be filtered out
    assert payload["retries"] == 1
    assert payload["state"] == "SUCCESS"
    assert payload["group"] is None
    assert payload["duration"] > 0

    hb.tearDown()


# Test context propagation
@patch("honeybadger.honeybadger.event")
@patch("honeybadger.honeybadger._get_event_context")
@patch("honeybadger.honeybadger.set_event_context")
def test_context_propagation(mock_set_context, mock_get_context, mock_event):
    """Test that context is properly propagated from publish to execution"""
    _, hb = setup_celery_hb()

    test_context = {"request_id": "test-123", "user_id": "456"}
    mock_get_context.return_value = test_context

    headers = {}
    hb._on_before_task_publish(headers=headers)

    assert headers["honeybadger_event_context"] == test_context

    task = MagicMock()
    task.request.honeybadger_event_context = test_context

    hb._on_task_prerun("test_task_id", task)

    mock_set_context.assert_called_once_with(test_context)

    hb.tearDown()


@patch("honeybadger.honeybadger.events_worker")
def test_worker_process_init(mock_events_worker):
    """Test that events worker is restarted in new worker process"""
    _, hb = setup_celery_hb()

    hb._on_worker_process_init()

    mock_events_worker.restart.assert_called_once()

    hb.tearDown()


# We configure a very short timeout so that shutdown() returns quickly after
# flushing — the worker sleeps events_timeout between flushes, so the default
# 5s would make the child process hang before os._exit().
@with_config({"events_timeout": 0.05})
@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork() not available")
def test_worker_process_init_without_insights():
    """Ensure that manual honeybadger.event() calls get sent within Celery tasks
    even when Insights is not enabled. Celery's (default) prefork worker pool forks
    worker processes via os.fork() and then fires a worker_process_init signal; our
    events worker must be restarted on worker_process_init to ensure that the
    events queue actually gets flushed. In other Celery worker pool configurations,
    this is not an issue."""
    from celery.signals import worker_process_init
    from honeybadger import honeybadger
    from honeybadger.types import EventsSendResult, EventsSendStatus

    app, hb = setup_celery_hb(insights_enabled=False)

    # Pipe to communicate which event types were actually flushed to the
    # connection. The child writes each event_type as it's sent; the parent
    # reads after the child exits (closing the write end, causing EOF).
    r_fd, w_fd = os.pipe()

    class CapturingConnection:
        def send_events(self, config, batch):
            for e in batch:
                os.write(w_fd, e.get("event_type", "").encode())
            return EventsSendResult(EventsSendStatus.OK, None)

    original_connection = honeybadger.events_worker.connection
    try:
        honeybadger.events_worker.connection = CapturingConnection()

        pid = os.fork()
        if pid == 0:
            # Child process: simulates a Celery forked worker process.
            # Python threads don't survive fork — the events worker thread is dead.
            os.close(r_fd)
            # Celery fires this signal in each forked worker process on startup.
            worker_process_init.send(sender=app)
            honeybadger.event("test.event", {"key": "value"})
            honeybadger.events_worker.shutdown()
            os.close(w_fd)
            os._exit(0)
        else:
            os.close(w_fd)
            os.waitpid(pid, 0)
            # Read until EOF — the write end closes when the child exits.
            with os.fdopen(r_fd, "rb") as f:
                result = f.read()
            assert b"test.event" in result
    finally:
        honeybadger.events_worker.connection = original_connection
        hb.tearDown()


@with_config({"events_timeout": 0.05})
def test_events_in_threads_mode():
    """Ensure events flush correctly when called from a Celery threads pool worker.
    Unlike prefork, the threads pool shares the same process so the events worker
    thread stays alive and no worker_process_init restart is needed."""
    from concurrent.futures import ThreadPoolExecutor
    from honeybadger import honeybadger
    from honeybadger.events_worker import EventsWorker
    from honeybadger.types import EventsSendResult, EventsSendStatus

    _, hb = setup_celery_hb(insights_enabled=False)

    sent = []

    class CapturingConnection:
        def send_events(self, config, batch):
            sent.extend(e.get("event_type", "") for e in batch)
            return EventsSendResult(EventsSendStatus.OK, None)

    previous_events_worker = honeybadger.events_worker
    try:
        honeybadger.events_worker = EventsWorker(CapturingConnection(), honeybadger.config)

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(honeybadger.event, "test.event", {"key": "value"}).result()

        honeybadger.events_worker.shutdown()

        assert "test.event" in sent
    finally:
        honeybadger.events_worker = previous_events_worker
        hb.tearDown()



@with_config({"insights_config": {"celery": {"disabled": True}}})
def test_can_disable():
    app, hb = setup_celery_hb()
    task = MagicMock()
    hb._on_task_postrun("test_task_id", task, None, state="SUCCESS")
    assert task.send_event.call_count == 0
    hb.tearDown()


@with_config({"insights_config": {"celery": {"exclude_tasks": ["test_task"]}}})
def test_exclude_tasks_with_string():
    app, hb = setup_celery_hb()
    task = MagicMock()
    task.name = "test_task"
    hb._on_task_postrun("test_task_id", task, None, state="SUCCESS")
    assert task.send_event.call_count == 0
    hb.tearDown()


@with_config(
    {"insights_config": {"celery": {"exclude_tasks": [re.compile(r"test_.*_task")]}}}
)
def test_exclude_tasks_with_regex():
    app, hb = setup_celery_hb()
    task = MagicMock()
    task.name = "test_the_task"
    hb._on_task_postrun("test_task_id", task, None, state="SUCCESS")
    assert task.send_event.call_count == 0
    hb.tearDown()
