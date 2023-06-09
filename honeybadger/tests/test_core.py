import json
import threading

from .utils import mock_urlopen
from honeybadger import Honeybadger
from mock import MagicMock, patch


def test_set_context():
    honeybadger = Honeybadger()
    honeybadger.set_context(foo='bar')
    assert honeybadger.thread_local.context == dict(foo='bar')
    honeybadger.set_context(bar='foo')
    assert honeybadger.thread_local.context == dict(foo='bar', bar='foo')


def test_set_context_with_dict():
    honeybadger = Honeybadger()
    honeybadger.set_context(dict(foo='bar'))
    assert honeybadger.thread_local.context == dict(foo='bar')
    honeybadger.set_context(dict(foo='bar', bar='foo'))
    assert honeybadger.thread_local.context == dict(foo='bar', bar='foo')


def test_threading():
    hb = Honeybadger()

    with patch('honeybadger.fake_connection.send_notice', side_effect=MagicMock(return_value=True)) as fake_connection:
        def notifier():
            try:
                raise ValueError('Failure')
            except ValueError as e:
                hb.notify(e)

        hb.configure(api_key='aaa')

        notify_thread = threading.Thread(target=notifier)
        notify_thread.start()
        notify_thread.join()
        assert fake_connection.call_count == 1


def test_notify_fake_connection_dev_environment():
    hb = Honeybadger()
    hb.configure(api_key='aaa')
    with patch('honeybadger.fake_connection.send_notice', side_effect=MagicMock(return_value=True)) as fake_connection:
        with patch('honeybadger.connection.send_notice', side_effect=MagicMock(return_value=True)) as connection:
            hb.notify(error_class='Exception', error_message='Test message.', context={'foo': 'bar'})

            assert fake_connection.call_count == 1
            assert connection.call_count == 0


def test_notify_fake_connection_dev_environment_with_force():
    hb = Honeybadger()
    hb.configure(api_key='aaa', force_report_data=True)
    with patch('honeybadger.fake_connection.send_notice', side_effect=MagicMock(return_value=True)) as fake_connection:
        with patch('honeybadger.connection.send_notice', side_effect=MagicMock(return_value=True)) as connection:
            hb.notify(error_class='Exception', error_message='Test message.', context={'foo': 'bar'})

            assert fake_connection.call_count == 0
            assert connection.call_count == 1


def test_notify_fake_connection_non_dev_environment():
    hb = Honeybadger()
    hb.configure(api_key='aaa', environment='production')
    with patch('honeybadger.fake_connection.send_notice', side_effect=MagicMock(return_value=True)) as fake_connection:
        with patch('honeybadger.connection.send_notice', side_effect=MagicMock(return_value=True)) as connection:
            hb.notify(error_class='Exception', error_message='Test message.', context={'foo': 'bar'})

            assert fake_connection.call_count == 0
            assert connection.call_count == 1


def test_notify_with_custom_params():
    def test_payload(request):
        payload = json.loads(request.data.decode('utf-8'))
        assert payload['request']['context'] == dict(foo='bar')
        assert payload['error']['class'] == 'Exception'
        assert payload['error']['message'] == 'Test message.'

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key='aaa', force_report_data=True)
        hb.notify(error_class='Exception', error_message='Test message.', context={'foo': 'bar'})


def test_notify_with_fingerprint():
    def test_payload(request):
        payload = json.loads(request.data.decode('utf-8'))
        assert payload['error']['class'] == 'Exception'
        assert payload['error']['fingerprint'] == 'custom_fingerprint'
        assert payload['error']['message'] == 'Test message.'

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key='aaa', force_report_data=True)
        hb.notify(error_class='Exception', error_message='Test message.', fingerprint='custom_fingerprint')


def test_notify_with_exception():
    def test_payload(request):
        payload = json.loads(request.data.decode('utf-8'))
        assert payload['error']['class'] == 'ValueError'
        assert payload['error']['message'] == 'Test value error.'

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key='aaa', force_report_data=True)
        hb.notify(ValueError('Test value error.'))


def test_notify_with_excluded_exception():
    def test_payload(request):
        payload = json.loads(request.data.decode('utf-8'))
        assert payload['error']['class'] == 'AttributeError'
        assert payload['error']['message'] == 'Test attribute error.'

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key='aaa', force_report_data=True, excluded_exceptions=['ValueError'])
        hb.notify(ValueError('Test value error.'))
        hb.notify(AttributeError('Test attribute error.'))


def test_notify_context_merging():
    def test_payload(request):
        payload = json.loads(request.data.decode('utf-8'))
        assert payload['request']['context'] == dict(foo='bar', bar='foo')

    hb = Honeybadger()

    with mock_urlopen(test_payload) as request_mock:
        hb.configure(api_key='aaa', force_report_data=True)
        hb.set_context(foo='bar')
        hb.notify(error_class='Exception', error_message='Test.', context=dict(bar='foo'))
