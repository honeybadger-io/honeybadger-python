import pytest
import time
from unittest.mock import patch, MagicMock

from celery import Celery
from celery.app.task import Context
from honeybadger import honeybadger
from honeybadger.contrib.celery import CeleryHoneybadger, CeleryPlugin

import honeybadger.connection as connection


@patch("honeybadger.honeybadger.reset_context")
@patch("honeybadger.honeybadger.notify")
def test_notify_from_task_failure(notify, reset_context):
    from celery.signals import task_failure, task_postrun

    app = Celery(__name__, broker="memory://")
    exception = Exception("Test exception")
    hb = CeleryHoneybadger(app, report_exceptions=True)

    # Send task_failure event
    task_failure.send(sender=app, task_id="hi", task_name="tasks.add", exception=exception)

    assert notify.call_count == 1
    assert notify.call_args[1]["exception"] == exception

    task_postrun.send(sender=app, task_id="hi", task_name="tasks.add", task={})

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
    test_task.request = Context(
        id="test_id",
        name="test_task",
        args=(1, 2),
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


@patch("celery.events.EventReceiver")
@patch("honeybadger.honeybadger.event")
def test_finished_task_event(mock_event, mock_event_receiver):
    from celery.signals import worker_ready, task_postrun

    app = Celery(__name__, broker="memory://localhost/")
    app.conf.update(
        HONEYBADGER_INSIGHTS_ENABLED=True,
        HONEYBADGER_API_KEY="test_api_key",
    )
    hb = CeleryHoneybadger(app)

    worker_ready.send(sender=app)

    # Give time for the thread to start
    time.sleep(0.5)

    assert mock_event_receiver.call_count == 1
    assert (
        mock_event_receiver.call_args[1]["handlers"]["task-finished"]
        == hb._on_task_finished
    )

    hb._on_task_finished({"payload": {"data": "test_task_id"}})

    assert mock_event.call_count == 1
    assert mock_event.call_args[0][0] == "celery.task_finished"
    assert mock_event.call_args[0][1] == {"data": "test_task_id"}

    hb.tearDown()
