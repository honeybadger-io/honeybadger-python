from six.moves import range
from six.moves import zip
from contextlib import contextmanager
import os
import sys

from honeybadger.payload import (
    create_payload,
    error_payload,
    server_payload
)
from honeybadger.config import Configuration

from mock import patch
from nose.tools import eq_, ok_, assert_raises

# TODO: figure out how to run Django tests?


@contextmanager
def mock_traceback(method='traceback.extract_stack', line_no=5):
    with patch(method) as traceback_mock:
        path = os.path.dirname(__file__)
        tb_data = []
        for i in range(1, 3):
            tb_data.append((os.path.join(path, 'file_{}.py'.format(i)), line_no*i, 'method_{}'.format(i)))

            tb_data.append(('/fake/path/fake_file.py', 15, 'fake_method'))
            tb_data.append((os.path.join(path, 'payload_fixture.txt'), line_no, 'fixture_method'))

        traceback_mock.return_value = tb_data
        yield traceback_mock


def test_error_payload_project_root_replacement():
    with mock_traceback() as traceback_mock:
        config = Configuration(project_root=os.path.dirname(__file__))
        payload = error_payload(dict(error_class='Exception', error_message='Test'), None, config)

        eq_(traceback_mock.call_count, 1)
        ok_(payload['backtrace'][0]['file'].startswith('[PROJECT_ROOT]'))
        eq_(payload['backtrace'][1]['file'], '/fake/path/fake_file.py')


def test_error_payload_source_line_top_of_file():
    with mock_traceback(line_no=1) as traceback_mock:
        config = Configuration()
        payload = error_payload(dict(error_class='Exception', error_message='Test'), None, config)
        expected = dict(zip(range(1, 5), ["Line {}\n".format(x) for x in range(1, 5)]))
        eq_(traceback_mock.call_count, 1)
        eq_(payload['backtrace'][0]['source'], expected)


def test_error_payload_source_line_bottom_of_file():
    with mock_traceback(line_no=10) as traceback_mock:
        config = Configuration()
        payload = error_payload(dict(error_class='Exception', error_message='Test'), None, config)
        expected = dict(zip(range(7, 11), ["Line {}\n".format(x) for x in range(7, 11)]))
        eq_(traceback_mock.call_count, 1)
        eq_(payload['backtrace'][0]['source'], expected)


def test_error_payload_source_line_midfile():
    with mock_traceback(line_no=5) as traceback_mock:
        config = Configuration()
        payload = error_payload(dict(error_class='Exception', error_message='Test'), None, config)
        expected = dict(zip(range(2, 9), ["Line {}\n".format(x) for x in range(2, 9)]))
        eq_(traceback_mock.call_count, 1)
        eq_(payload['backtrace'][0]['source'], expected)


@patch('os.path.isfile', return_value=False)
def test_error_payload_source_missing_file(_isfile):
    with mock_traceback(line_no=5) as traceback_mock:
        config = Configuration()
        payload = error_payload(
            dict(error_class='Exception', error_message='Test'), None, config)
        eq_(payload['backtrace'][0]['source'], {})


def test_payload_captures_exception_cause():
    with mock_traceback() as traceback_mock:
        config = Configuration()
        exception = Exception('Test')
        exception.__cause__ = Exception('Exception cause')

        payload = error_payload(exc_traceback=None, exception=exception,  config=config)
        eq_(len(payload['causes']), 1)


def test_error_payload_with_nested_exception():
    with mock_traceback() as traceback_mock:
        config = Configuration()
        exception = Exception('Test')
        exception_cause = Exception('Exception cause')
        exception_cause.__cause__ = Exception('Nested')
        exception.__cause__ = exception_cause
        payload = error_payload(exc_traceback=None, exception=exception,  config=config)
        eq_(len(payload['causes']), 2)


def test_error_payload_with_fingerprint():
    config = Configuration()
    exception = Exception('Test')
    payload = error_payload(exception, exc_traceback=None, config=config, fingerprint='a fingerprint')
    eq_(payload['fingerprint'], 'a fingerprint')


def test_error_payload_with_fingerprint_as_type():
    config = Configuration()
    exception = Exception('Test')
    payload = error_payload(exception, exc_traceback=None, config=config, fingerprint={'a': 1, 'b': 2})
    eq_(payload['fingerprint'], "{'a': 1, 'b': 2}")


def test_error_payload_without_fingerprint():
    config = Configuration()
    exception = Exception('Test')
    payload = error_payload(exception, exc_traceback=None, config=config)
    eq_(payload.get('fingerprint'), None)


def test_server_payload():
    config = Configuration(project_root=os.path.dirname(__file__), environment='test', hostname='test.local')
    payload = server_payload(config)

    eq_(payload['project_root'], os.path.dirname(__file__))
    eq_(payload['environment_name'], 'test')
    eq_(payload['hostname'], 'test.local')
    eq_(payload['pid'], os.getpid())
    assert type(payload['stats']['mem']['total']) == float
    assert type(payload['stats']['mem']['free']) == float


def test_psutil_is_optional():
    config = Configuration()

    with patch.dict(sys.modules, {'psutil': None}):
        payload = server_payload(config)
        eq_(payload['stats'], {})


def test_create_payload_without_local_variables():
    config = Configuration()
    exception = Exception('Test')
    payload = create_payload(exception, config=config)
    eq_(payload['request'].get('local_variables'), None)


def test_create_payload_with_local_variables():
    config = Configuration(report_local_variables=True)
    with assert_raises(Exception):
        test_local_variable = {"test": "var"}
        exception = Exception('Test')
        payload = create_payload(exception, config=config)
        eq_(payload['request']['local_variables'], test_local_variable)
