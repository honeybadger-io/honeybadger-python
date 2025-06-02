from honeybadger.utils import filter_dict, filter_env_vars, sanitize_request_id


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


def test_sanitize_request_id():
    assert sanitize_request_id("abc123-def456") == "abc123-def456"
    assert sanitize_request_id("abc_123@def#456") == "abc123def456"
    assert sanitize_request_id("a" * 300) == "a" * 255
    assert sanitize_request_id("  abc123  ") == "abc123"
    assert sanitize_request_id("@#$%^&*()") is None
    assert sanitize_request_id(None) is None
    assert sanitize_request_id("") is None
    assert sanitize_request_id("   ") is None
