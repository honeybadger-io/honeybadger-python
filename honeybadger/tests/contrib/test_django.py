import unittest
import json
from mock import patch
from mock import Mock
import sys

from django.conf.urls import url
from django.conf import settings
from django.test import RequestFactory
from django.test import SimpleTestCase
from django.test import override_settings
from django.test import modify_settings
from django.test import Client

from honeybadger import honeybadger
from honeybadger.config import Configuration
from honeybadger.contrib import DjangoHoneybadgerMiddleware
from honeybadger.contrib.django import DjangoPlugin
from honeybadger.contrib.django import clear_request
from honeybadger.contrib.django import set_request
from honeybadger.contrib.django import current_request

from .django_test_app.views import plain_view
from .django_test_app.views import always_fails
from ..utils import mock_urlopen

try:
    settings.configure()
except:
    pass

def versions_match():
    import django

    VERSION_MATRIX = {
        '1.11': sys.version_info >= (3, 5),
        '2.2':  sys.version_info >= (3, 5),
        '3.0':  sys.version_info >= (3, 6),
        '3.1':  sys.version_info >= (3, 6)
    }

    for django_version, supported in VERSION_MATRIX.items():
        if django.__version__.startswith(django_version) and supported:
            return True
    return False

class DjangoPluginTestCase(unittest.TestCase):
    def setUp(self):
        self.plugin = DjangoPlugin()
        self.rf = RequestFactory()
        self.config = Configuration()
        self.url = url(r'test', plain_view, name='test_view')
        self.default_payload = {}

    def tearDown(self):
        clear_request()

    def test_supports_django_request(self):
        request = self.rf.get('test')
        set_request(request)

        self.assertTrue(self.plugin.supports(self.config, {}))

    def test_generate_payload_get(self):
        request = self.rf.get('test', {'a': 1})
        request.resolver_match = self.url.resolve('test')
        set_request(request)

        payload = self.plugin.generate_payload(self.default_payload, self.config, {'foo': 'bar'})
        self.assertEqual(payload['request']['url'], 'http://testserver/test?a=1')
        self.assertEqual(payload['request']['action'], 'plain_view')
        self.assertDictEqual(payload['request']['params'], {'a': ['1']})
        self.assertDictEqual(payload['request']['session'], {})
        self.assertDictEqual(payload['request']['context'], {'foo': 'bar'})

    def test_generate_payload_post(self):
        request = self.rf.post('test', data={'a': 1, 'b': 2, 'password': 'notsafe'})
        request.resolver_match = self.url.resolve('test')
        set_request(request)

        payload = self.plugin.generate_payload(self.default_payload, self.config, {'foo': 'bar'})
        self.assertEqual(payload['request']['url'], 'http://testserver/test')
        self.assertEqual(payload['request']['action'], 'plain_view')
        self.assertDictEqual(payload['request']['params'], {'a': ['1'], 'b': ['2'], 'password': '[FILTERED]'})
        self.assertDictEqual(payload['request']['session'], {})
        self.assertDictEqual(payload['request']['context'], {'foo': 'bar'})

    def test_generate_payload_with_session(self):
        request = self.rf.get('test')
        request.resolver_match = self.url.resolve('test')
        request.session = {'lang': 'en'}
        set_request(request)

        payload = self.plugin.generate_payload(self.default_payload, self.config, {'foo': 'bar'})
        self.assertEqual(payload['request']['url'], 'http://testserver/test')
        self.assertEqual(payload['request']['action'], 'plain_view')
        self.assertDictEqual(payload['request']['session'], {'lang': 'en'})
        self.assertDictEqual(payload['request']['context'], {'foo': 'bar'})

# TODO: add an integration test case that tests the actual integration with Django

class DjangoMiddlewareTestCase(unittest.TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.url = url(r'test', plain_view, name='test_view')

    def tearDown(self):
        clear_request()

    def get_response(self, request):
        return Mock()

    @patch('honeybadger.contrib.django.honeybadger')
    def test_process_exception(self, mock_hb):
        request = self.rf.get('test')
        request.resolver_match = self.url.resolve('test')
        exc = ValueError('test exception')

        middleware = DjangoHoneybadgerMiddleware(self.get_response)
        middleware.process_exception(request, exc)

        mock_hb.notify.assert_called_with(exc)
        self.assertIsNone(current_request(), msg='Current request should be cleared after exception handling')

    def test___call__(self):
        request = self.rf.get('test')
        request.resolver_match = self.url.resolve('test')

        middleware = DjangoHoneybadgerMiddleware(self.get_response)
        response = middleware(request)

        self.assertDictEqual({}, honeybadger._get_context(), msg='Context should be cleared after response handling')
        self.assertIsNone(current_request(), msg='Current request should be cleared after response handling')


@override_settings(
    ROOT_URLCONF='honeybadger.tests.contrib.django_test_app.urls',
    MIDDLEWARE=['honeybadger.contrib.django.DjangoHoneybadgerMiddleware']
)
class DjangoMiddlewareIntegrationTestCase(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    @unittest.skipUnless(versions_match(), "Current Python version unsupported by current version of Django")
    def test_context_cleared_after_response(self):
        self.assertIsNone(current_request(), msg='Current request should be empty prior to request')
        response = self.client.get('/plain_view')
        self.assertIsNone(current_request(), msg='Current request should be cleared after request processed')

    @unittest.skipUnless(versions_match(), "Current Python version unsupported by current version of Django")
    @override_settings(
        HONEYBADGER={
            'API_KEY': 'abc123'
        }
    )
    def test_exceptions_handled_by_middleware(self):
        def assert_payload(req):
            error_payload = json.loads(str(req.data, "utf-8"))

            self.assertEqual(req.get_header('X-api-key'), 'abc123')
            self.assertEqual(req.get_full_url(), '{}/v1/notices/'.format(honeybadger.config.endpoint))
            self.assertEqual(error_payload['error']['class'], 'ValueError')
            self.assertEqual(error_payload['error']['message'], 'always fails')


        with mock_urlopen(assert_payload) as request_mock:
            try:
                response = self.client.get('/always_fails/')
            except:
                pass
            self.assertTrue(request_mock.called)
