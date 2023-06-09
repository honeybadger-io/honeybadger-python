from contextlib import contextmanager
from mock import patch
from mock import DEFAULT
import six
import time
from threading import Event


@contextmanager
def mock_urlopen(func, status=201):
    mock_called_event = Event()

    def mock_was_called(*args, **kwargs):
        mock_called_event.set()
        return DEFAULT

    with patch('six.moves.urllib.request.urlopen', side_effect=mock_was_called) as request_mock:
        yield request_mock
        mock_called_event.wait(0.5)
        ((request_object,), mock_kwargs) = request_mock.call_args
        func(request_object)
