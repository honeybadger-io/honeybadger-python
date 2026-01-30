import json
import threading

import pytest
import asyncio

from .utils import mock_urlopen
from honeybadger import Honeybadger
from mock import MagicMock, patch
from honeybadger.config import Configuration


def test_set_and_get_context_merges_values():
    hb = Honeybadger()
    assert hb._get_context() == {}

    hb.set_context(foo="bar")
    hb.set_context(baz=123)
    assert hb._get_context() == {"foo": "bar", "baz": 123}

    hb.set_context({"a": 1})
    assert hb._get_context() == {"foo": "bar", "baz": 123, "a": 1}


def test_reset_context_clears_all():
    hb = Honeybadger()
    hb.set_context(temp="value")
    assert hb._get_context()  # non-empty
    hb.reset_context()
    assert hb._get_context() == {}


def test_context_manager_pushes_and_pops():
    hb = Honeybadger()
    hb.set_context(x=1)
    original = hb._get_context()

    with hb.context(y=2):
        # inside block, we see both x and y
        assert hb._get_context() == {"x": 1, "y": 2}

    # after block, y is gone
    assert hb._get_context() == original


def test_thread_isolation():
    hb = Honeybadger()
    hb.set_context(main=True)

    def worker():
        # new thread should start with empty context
        assert hb._get_context() == {}
        hb.set_context(thread="worker")
        assert hb._get_context() == {"thread": "worker"}

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    # main thread context is untouched
    assert hb._get_context() == {"main": True}


@pytest.mark.asyncio
async def test_context_async_isolation():
    hb = Honeybadger()
    hb.set_context(main=True)
    assert hb._get_context() == {"main": True}

    async def worker():
        assert hb._get_context() == {"main": True}
        hb.set_context(thread="worker")
        assert hb._get_context() == {"main": True, "thread": "worker"}

    tasks = [asyncio.create_task(worker()) for _ in range(2)]
    await asyncio.gather(*tasks)
    assert hb._get_context() == {"main": True}


def test_notify_merges_context_and_tags(monkeypatch):
    hb = Honeybadger()
    hb.set_context(user="alice", _tags=["from_ctx, another_tag"])
    captured = {}

    def fake_send(notice):
        captured["context"] = notice.context
        captured["tags"] = notice.tags

    monkeypatch.setattr(hb, "_send_notice", fake_send)

    hb.notify(
        exception=RuntimeError("oops"),
        context={"action": "save"},
        tags=["explicit"],
    )

    # should merge store + explicit
    assert captured["context"] == {"user": "alice", "action": "save"}
    # tags deduped and merged
    assert set(captured["tags"]) == {"from_ctx", "another_tag", "explicit"}


def test_exception_hook_calls_notify(monkeypatch):
    hb = Honeybadger()
    captured = {}

    def fake_send(notice=None, **kwargs):
        captured["notified"] = notice
        return "sent"

    def fake_existing_except_hook(*args, **kwargs):
        captured["hook"] = True

    monkeypatch.setattr(hb, "_send_notice", fake_send)

    exc = ValueError("fail!")
    hb.wrap_excepthook(fake_existing_except_hook)
    hb.exception_hook(ValueError, exc, None)

    assert captured["notified"].exception == exc
    assert captured["hook"] is True


def test_threading():
    hb = Honeybadger()
    hb.configure(api_key="aaa", environment="development")  # Explicitly use development

    # Patch both possible connection functions
    with patch(
        "honeybadger.fake_connection.send_notice",
        side_effect=MagicMock(return_value=True),
    ) as fake_connection, patch(
        "honeybadger.connection.send_notice",
        side_effect=MagicMock(return_value=True),
    ) as connection:

        def notifier():
            try:
                raise ValueError("Failure")
            except ValueError as e:
                hb.notify(e)

        notify_thread = threading.Thread(target=notifier)
        notify_thread.start()
        notify_thread.join()

        # Check if either connection was used
        assert fake_connection.call_count == 1
        assert connection.call_count == 0


def test_notify_fake_connection_dev_environment():
    hb = Honeybadger()
    hb.configure(api_key="aaa", environment="development")
    with patch(
        "honeybadger.fake_connection.send_notice",
        side_effect=MagicMock(return_value=True),
    ) as fake_connection, patch(
        "honeybadger.connection.send_notice",
        side_effect=MagicMock(return_value=True),
    ) as connection:
        hb.notify(
            error_class="Exception",
            error_message="Test message.",
            context={"foo": "bar"},
        )

        assert fake_connection.call_count == 1
        assert connection.call_count == 0


def test_notify_fake_connection_dev_environment_with_force():
    hb = Honeybadger()
    hb.configure(api_key="aaa", force_report_data=True)
    with patch(
        "honeybadger.fake_connection.send_notice",
        side_effect=MagicMock(return_value=True),
    ) as fake_connection:
        with patch(
            "honeybadger.connection.send_notice",
            side_effect=MagicMock(return_value=True),
        ) as connection:
            hb.notify(
                error_class="Exception",
                error_message="Test message.",
                context={"foo": "bar"},
            )

            assert fake_connection.call_count == 0
            assert connection.call_count == 1


def test_notify_fake_connection_non_dev_environment():
    hb = Honeybadger()
    hb.configure(api_key="aaa", environment="production")
    with patch(
        "honeybadger.fake_connection.send_notice",
        side_effect=MagicMock(return_value=True),
    ) as fake_connection:
        with patch(
            "honeybadger.connection.send_notice",
            side_effect=MagicMock(return_value=True),
        ) as connection:
            hb.notify(
                error_class="Exception",
                error_message="Test message.",
                context={"foo": "bar"},
            )

            assert fake_connection.call_count == 0
            assert connection.call_count == 1


def test_before_notify_with_none_return_value():
    def before_notify(notice):
        return None

    hb = Honeybadger()
    hb.configure(api_key="aaa", environment="development", before_notify=before_notify)
    with patch(
        "honeybadger.fake_connection.send_notice",
        side_effect=MagicMock(return_value=True),
    ) as fake_connection:
        hb.notify(
            error_class="Exception",
            error_message="Test message.",
            context={"foo": "bar"},
        )

        assert fake_connection.call_count == 0


def test_notify_with_custom_params():
    def test_payload(request):
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["request"]["context"] == dict(foo="bar")
        assert payload["error"]["class"] == "Exception"
        assert payload["error"]["message"] == "Test message."

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key="aaa", force_report_data=True)
        hb.notify(
            error_class="Exception",
            error_message="Test message.",
            context={"foo": "bar"},
        )


def test_notify_with_fingerprint():
    def test_payload(request):
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["error"]["class"] == "Exception"
        assert payload["error"]["fingerprint"] == "custom_fingerprint"
        assert payload["error"]["message"] == "Test message."

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key="aaa", force_report_data=True)
        hb.notify(
            error_class="Exception",
            error_message="Test message.",
            fingerprint="custom_fingerprint",
        )


def test_notify_with_exception():
    def test_payload(request):
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["error"]["class"] == "ValueError"
        assert payload["error"]["message"] == "Test value error."

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key="aaa", force_report_data=True)
        hb.notify(ValueError("Test value error."))


def test_notify_with_excluded_exception():
    def test_payload(request):
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["error"]["class"] == "AttributeError"
        assert payload["error"]["message"] == "Test attribute error."

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(
            api_key="aaa", force_report_data=True, excluded_exceptions=["ValueError"]
        )
        hb.notify(ValueError("Test value error."))
        hb.notify(AttributeError("Test attribute error."))


def test_notify_context_merging():
    def test_payload(request):
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["request"]["context"] == dict(foo="bar", bar="foo")

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key="aaa", force_report_data=True)
        hb.set_context(foo="bar")
        hb.notify(
            error_class="Exception", error_message="Test.", context=dict(bar="foo")
        )


def test_notify_with_request_id():
    def test_payload(request):
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["correlation_context"]["request_id"] == "12345"

    hb = Honeybadger()
    hb.set_event_context(request_id="12345")

    with mock_urlopen(test_payload):
        hb.configure(api_key="aaa", force_report_data=True)
        hb.notify(error_class="Exception", error_message="Test message.")


def test_event_with_two_params():
    mock_events_worker = MagicMock()

    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True)
    hb.event(event_type="order.completed", data=dict(email="user@example.com"))

    mock_events_worker.push.assert_called_once()
    payload = mock_events_worker.push.call_args[0][0]

    assert "ts" in payload
    assert payload["event_type"] == "order.completed"
    assert payload["email"] == "user@example.com"


def test_event_with_one_param():
    mock_events_worker = MagicMock()

    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True)
    hb.event(dict(event_type="order.completed", email="user@example.com"))

    mock_events_worker.push.assert_called_once()
    payload = mock_events_worker.push.call_args[0][0]

    assert "ts" in payload
    assert payload["event_type"] == "order.completed"
    assert payload["email"] == "user@example.com"


def test_event_without_event_type():
    mock_events_worker = MagicMock()

    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True)
    hb.event(dict(email="user@example.com"))

    mock_events_worker.push.assert_called_once()
    payload = mock_events_worker.push.call_args[0][0]

    assert "ts" in payload
    assert payload["email"] == "user@example.com"


def test_event_with_event_context():
    mock_events_worker = MagicMock()

    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True)
    hb.set_event_context(service="web")
    hb.event(event_type="order.completed", data=dict(email="user@example.com"))

    mock_events_worker.push.assert_called_once()
    payload = mock_events_worker.push.call_args[0][0]

    assert payload["service"] == "web"
    assert payload["email"] == "user@example.com"


def test_event_with_event_context_does_not_override():
    mock_events_worker = MagicMock()

    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True)
    hb.set_event_context(service="web")
    hb.event(event_type="order.completed", data=dict(service="my-service!"))

    mock_events_worker.push.assert_called_once()
    payload = mock_events_worker.push.call_args[0][0]

    assert payload["service"] == "my-service!"


def test_set_and_get_event_context_merges_values():
    hb = Honeybadger()
    assert hb._get_event_context() == {}

    hb.set_event_context(foo="bar")
    hb.set_event_context(baz=123)
    assert hb._get_event_context() == {"foo": "bar", "baz": 123}

    hb.set_event_context({"a": 1})
    assert hb._get_event_context() == {"foo": "bar", "baz": 123, "a": 1}


def test_reset_event_context_clears_all():
    hb = Honeybadger()
    hb.set_event_context(temp="value")
    assert hb._get_event_context()  # non-empty
    hb.reset_event_context()
    assert hb._get_event_context() == {}


def test_event_context_manager_pushes_and_pops():
    hb = Honeybadger()
    hb.set_event_context(x=1)
    original = hb._get_event_context()

    with hb.event_context(y=2):
        # inside block, we see both x and y
        assert hb._get_event_context() == {"x": 1, "y": 2}

    # after block, y is gone
    assert hb._get_event_context() == original


def test_event_context_thread_isolation():
    hb = Honeybadger()
    hb.set_event_context(main=True)

    def worker():
        # new thread should start with empty event_context
        assert hb._get_event_context() == {}
        hb.set_event_context(thread="worker")
        assert hb._get_event_context() == {"thread": "worker"}

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    # main thread event_context is untouched
    assert hb._get_event_context() == {"main": True}


@pytest.mark.asyncio
async def test_event_context_async_isolation():
    hb = Honeybadger()
    hb.set_event_context(main=True)
    assert hb._get_event_context() == {"main": True}

    async def worker():
        assert hb._get_event_context() == {"main": True}
        hb.set_event_context(thread="worker")
        assert hb._get_event_context() == {"main": True, "thread": "worker"}

    tasks = [asyncio.create_task(worker()) for _ in range(2)]
    await asyncio.gather(*tasks)
    assert hb._get_event_context() == {"main": True}


def test_event_with_before_event_mutated_changes():
    def before_event(event):
        if "ignore" in event:
            return False
        event["new_key"] = "new_value"

    mock_events_worker = MagicMock()
    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True, before_event=before_event)
    hb.event(dict(email="user@example.com"))
    hb.event(dict(ignore="yeah"))

    mock_events_worker.push.assert_called_once()
    payload = mock_events_worker.push.call_args[0][0]
    assert len(mock_events_worker.push.call_args[0]) == 1

    assert "ts" in payload
    assert payload["new_key"] == "new_value"
    hb.config = Configuration()


def test_event_with_before_event_returned_changes():
    def before_event(event):
        return {
            "new_key": "new_value",
        }

    mock_events_worker = MagicMock()
    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True, before_event=before_event)
    hb.event(dict(a="b"))

    mock_events_worker.push.assert_called_once()
    payload = mock_events_worker.push.call_args[0][0]
    assert "ts" in payload
    assert payload["new_key"] == "new_value"
    assert "a" not in payload

    hb.config = Configuration()


def test_notify_with_before_notify_changes():
    def before_notify(notice):
        notice.payload["error"]["tags"] = ["tag1-updated"]
        return notice

    def test_payload(request):
        payload = json.loads(request.data.decode("utf-8"))
        assert sorted(payload["error"]["tags"]) == sorted(["tag1-updated"])

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key="aaa", force_report_data=True, before_notify=before_notify)
        hb.notify(
            error_class="Exception",
            error_message="Test.",
            context=dict(bar="foo"),
            tags="tag1",
        )


@pytest.mark.parametrize(
    "sample_rate,expected_calls",
    [
        (0, 0),  # Never send
        (100, 1),  # Always send
        (-10, 0),  # Negative = never send
    ],
)
def test_event_sampling_rates(sample_rate, expected_calls):
    """Test various sample rates"""
    mock_events_worker = MagicMock()
    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True, events_sample_rate=sample_rate)

    hb.event("test.event", {"data": "test"})
    assert mock_events_worker.push.call_count == expected_calls


def test_event_sampling_precedence_and_cleanup():
    """Test that _hb overrides work and get stripped from payload"""
    mock_events_worker = MagicMock()
    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(
        api_key="aaa", force_report_data=True, events_sample_rate=0
    )  # Global: never send

    # Context override: should send
    hb.set_event_context(_hb={"sample_rate": 100}, service="web")
    hb.event("test.event1", {"data": "test1"})
    assert mock_events_worker.push.call_count == 1

    # Event data override: should not send (overrides context)
    hb.event("test.event2", {"data": "test2", "_hb": {"sample_rate": 0}})
    assert mock_events_worker.push.call_count == 1  # Still 1, no new call

    # Verify _hb is stripped and other data preserved
    payload = mock_events_worker.push.call_args[0][0]
    assert "_hb" not in payload
    assert payload["service"] == "web"
    assert payload["data"] == "test1"


def test_notify_with_error_message_only():
    """Test the case where only an error message is provided"""

    def test_payload(request):
        payload = json.loads(request.data.decode("utf-8"))
        # This demonstrates the current bug - NoneType class and None message
        assert payload["error"]["class"] == "dict"
        assert payload["error"]["message"] == "Some error message"

    hb = Honeybadger()

    with mock_urlopen(test_payload):
        hb.configure(api_key="aaa", force_report_data=True)
        # This should fail according to breaking change, but currently creates malformed payload
        hb.notify(error_message="Some error message", context={"foo": "bar"})


def test_notify_returns_notice_id():
    """Test that notify() returns a notice_id (UUID) when sending succeeds"""
    hb = Honeybadger()
    hb.configure(api_key="aaa", environment="production")

    with patch(
        "honeybadger.connection.send_notice",
        return_value="test-notice-uuid-123",
    ) as mock_send:
        result = hb.notify(
            error_class="Exception",
            error_message="Test message.",
        )

        assert mock_send.call_count == 1
        assert result == "test-notice-uuid-123"


def test_notify_returns_none_when_filtered_by_before_notify():
    """Test that notify() returns None when before_notify filters out the notice"""

    def before_notify(notice):
        return None  # Filter out the notice

    hb = Honeybadger()
    hb.configure(api_key="aaa", environment="production", before_notify=before_notify)

    with patch("honeybadger.connection.send_notice") as mock_send:
        result = hb.notify(
            error_class="Exception",
            error_message="Test message.",
        )

        assert mock_send.call_count == 0
        assert result is None


def test_notify_returns_none_when_exception_excluded():
    """Test that notify() returns None when the exception is excluded"""
    hb = Honeybadger()
    hb.configure(
        api_key="aaa",
        environment="production",
        excluded_exceptions=["ValueError"],
    )

    with patch("honeybadger.connection.send_notice") as mock_send:
        result = hb.notify(ValueError("excluded error"))

        assert mock_send.call_count == 0
        assert result is None
