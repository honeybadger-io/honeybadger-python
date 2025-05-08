import pytest
import threading

from honeybadger.config import Configuration
from honeybadger.notice import Notice


def test_notice_initialization():
    # Test with exception
    exception = Exception("Test exception")
    notice = Notice(exception=exception)
    assert notice.exception == exception
    assert notice.error_class is None
    assert notice.error_message is None

    # Test with error_class and error_message
    notice = Notice(error_class="TestError", error_message="Test message")
    assert notice.exception == {
        "error_class": "TestError",
        "error_message": "Test message",
    }
    assert notice.error_class == "TestError"
    assert notice.error_message == "Test message"

    # Test with neither exception nor error_class
    with pytest.raises(ValueError):
        Notice()

    # Test with exception and error_message
    exception = Exception("Test exception")
    notice = Notice(exception=exception, error_message="Test message")
    assert notice.exception == exception
    assert notice.context["error_message"] == "Test message"


def test_notice_excluded_exception():
    config = Configuration(excluded_exceptions=["TestError", "Exception"])

    # Test with excluded exception
    notice = Notice(
        error_class="TestError", error_message="Test message", config=config
    )
    assert notice.excluded_exception() is True

    # Test with non-excluded exception
    notice = Notice(
        error_class="NonExcludedError", error_message="Test message", config=config
    )
    assert notice.excluded_exception() is False

    # Test with exception
    notice = Notice(exception=Exception("Test exception"), config=config)
    assert notice.excluded_exception() is True


def test_notice_payload():
    config = Configuration()

    # Test with exception
    notice = Notice(exception=Exception("Test exception"), config=config)
    payload = notice.payload
    assert payload["error"]["class"] == "Exception"
    assert payload["error"]["message"] == "Test exception"

    # Test with error_class and error_message
    notice = Notice(
        error_class="TestError", error_message="Test message", config=config
    )
    payload = notice.payload
    assert payload["error"]["class"] == "TestError"
    assert payload["error"]["message"] == "Test message"


def test_notice_with_tags():
    config = Configuration()

    notice = Notice(
        error_class="TestError",
        error_message="Test message",
        tags="tag1, tag2",
        config=config,
    )
    payload = notice.payload
    assert "tag1" in payload["error"]["tags"]
    assert "tag2" in payload["error"]["tags"]


def test_notify_with_context_tags():
    config = Configuration()
    thread_local = threading.local()
    thread_local.context = {"_tags": "tag1, tag2"}

    notice = Notice(
        error_class="TestError",
        error_message="Test message",
        thread_local=thread_local,
        config=config,
    )
    payload = notice.payload
    assert "tag1" in payload["error"]["tags"]
    assert "tag2" in payload["error"]["tags"]


def test_notify_with_context_merging_tags():
    config = Configuration()
    thread_local = threading.local()
    thread_local.context = {"_tags": "tag1"}

    notice = Notice(
        error_class="TestError",
        error_message="Test message",
        thread_local=thread_local,
        tags="tag2",
        config=config,
    )
    payload = notice.payload
    assert "tag1" in payload["error"]["tags"]
    assert "tag2" in payload["error"]["tags"]
