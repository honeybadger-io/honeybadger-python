import time
from unittest.mock import patch

from honeybadger.utils import (
    filter_dict,
    filter_env_vars,
    filter_structure,
    get_duration,
    sanitize_request_id,
)


def test_filter_dict():
    data = {"foo": "bar", "bar": "baz"}
    expected = {"foo": "[FILTERED]", "bar": "baz"}
    filter_keys = ["foo"]
    assert filter_dict(data, filter_keys) == expected


def test_filter_dict_with_nested_dict():
    data = {"foo": "bar", "bar": "baz", "nested": {"password": "helloworld"}}
    expected = {"foo": "bar", "bar": "baz", "nested": {"password": "[FILTERED]"}}
    filter_keys = ["password"]
    assert filter_dict(data, filter_keys) == expected


def test_ignores_dict_with_tuple_key():
    data = {("foo", "bar"): "baz", "key": "value"}
    expected = {"key": "value"}
    filter_keys = ["foo"]
    assert filter_dict(data, filter_keys) == expected


def test_filter_env_vars_with_http_prefix():
    data = {
        "HTTP_HOST": "example.com",
        "HTTP_USER_AGENT": "Mozilla",
        "PATH": "/usr/bin",
        "TERM": "xterm",
    }
    expected = {"HTTP_HOST": "example.com", "HTTP_USER_AGENT": "Mozilla"}
    assert filter_env_vars(data) == expected


def test_filter_env_vars_with_cgi_allowlist():
    data = {
        "CONTENT_LENGTH": "256",
        "REMOTE_ADDR": "127.0.0.1",
        "SERVER_NAME": "localhost",
        "DATABASE_URL": "postgres://localhost",
        "AWS_SECRET_KEY": "secret123",
    }
    expected = {
        "CONTENT_LENGTH": "256",
        "REMOTE_ADDR": "127.0.0.1",
        "SERVER_NAME": "localhost",
    }
    assert filter_env_vars(data) == expected


def test_filter_env_vars_with_mixed_vars():
    data = {
        "HTTP_HOST": "example.com",
        "CONTENT_LENGTH": "256",
        "AWS_SECRET_KEY": "secret123",
        "DATABASE_URL": "postgres://localhost",
        "PATH": "/usr/bin",
    }
    expected = {"HTTP_HOST": "example.com", "CONTENT_LENGTH": "256"}
    assert filter_env_vars(data) == expected


def test_filter_env_vars_with_non_dict():
    assert filter_env_vars(None) is None
    assert filter_env_vars([]) == []
    assert filter_env_vars("string") == "string"


def test_filter_env_vars_empty_dict():
    assert filter_env_vars({}) == {}


def test_get_duration_returns_milliseconds():
    start = time.monotonic()
    time.sleep(0.05)
    duration = get_duration(start)
    assert isinstance(duration, float)
    assert 30 <= duration <= 200


def test_get_duration_returns_none_for_none():
    assert get_duration(None) is None


def test_get_duration_uses_monotonic():
    with patch("honeybadger.utils.time") as mock_time:
        mock_time.monotonic.return_value = 1000.150
        result = get_duration(1000.050)
        mock_time.monotonic.assert_called_once()
        assert result == 100.0


def test_sanitize_request_id():
    assert sanitize_request_id("abc123-def456") == "abc123-def456"
    assert sanitize_request_id("abc_123@def#456") == "abc123def456"
    assert sanitize_request_id("a" * 300) == "a" * 255
    assert sanitize_request_id("  abc123  ") == "abc123"
    assert sanitize_request_id("@#$%^&*()") is None
    assert sanitize_request_id(None) is None
    assert sanitize_request_id("") is None
    assert sanitize_request_id("   ") is None


def test_filter_structure_filters_keys_inside_lists():
    data = {"messages": [{"role": "user", "password": "hunter2", "content": "hi"}]}
    result = filter_structure(data, ["password"])
    assert result["messages"][0]["password"] == "[FILTERED]"
    assert result["messages"][0]["content"] == "hi"


def test_filter_structure_does_not_mutate_input():
    data = {"outer": [{"password": "hunter2"}]}
    filter_structure(data, ["password"])
    assert data["outer"][0]["password"] == "hunter2"


def test_filter_structure_handles_nested_dicts_and_scalars():
    data = {"a": {"password": "x", "b": [1, "two", {"password": "y"}]}}
    result = filter_structure(data, ["password"])
    assert result["a"]["password"] == "[FILTERED]"
    assert result["a"]["b"][2]["password"] == "[FILTERED]"
    assert result["a"]["b"][0] == 1


def test_filter_structure_drops_tuple_keys():
    data = {("a", "b"): 1, "keep": 2}
    result = filter_structure(data, [])
    assert result == {"keep": 2}


def test_filter_structure_passes_through_non_containers():
    assert filter_structure("plain", ["password"]) == "plain"
    assert filter_structure(None, ["password"]) is None
