from contextlib import contextmanager
from mock import patch
from mock import DEFAULT
import inspect
import six
import time
from functools import wraps
from threading import Event
from honeybadger import honeybadger
from honeybadger.config import Configuration


@contextmanager
def mock_urlopen(func, status=201):
    mock_called_event = Event()

    def mock_was_called(*args, **kwargs):
        mock_called_event.set()
        return DEFAULT

    with patch(
        "six.moves.urllib.request.urlopen", side_effect=mock_was_called
    ) as request_mock:
        yield request_mock
        mock_called_event.wait(0.5)
        ((request_object,), mock_kwargs) = request_mock.call_args
        func(request_object)


def with_config(config):
    """
    Decorator to set honeybadger.config for a test, and restore it after.
    Usage:
        @with_config({"a": "b"})
        def test_...():
            ...
    """

    def decorator(fn):
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def wrapper(*args, **kwargs):
                honeybadger.configure(**config)
                try:
                    return await fn(*args, **kwargs)
                finally:
                    honeybadger.config = Configuration()

        else:

            @wraps(fn)
            def wrapper(*args, **kwargs):
                honeybadger.configure(**config)
                try:
                    return fn(*args, **kwargs)
                finally:
                    honeybadger.config = Configuration()

        return wrapper

    return decorator
