import time
import re
from unittest.mock import patch, MagicMock

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


def setup_celery_hb():
    from celery.signals import worker_ready

    app = Celery(__name__, broker="memory://localhost/")
    app.conf.update(
        HONEYBADGER_INSIGHTS_ENABLED=True,
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
