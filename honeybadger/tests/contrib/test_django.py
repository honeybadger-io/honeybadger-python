import unittest

from django.conf.urls import url
from django.http import JsonResponse
from django.test import RequestFactory

from honeybadger.config import Configuration
from honeybadger.contrib.django import DjangoPlugin, clear_request, set_request


def view(request):
    # Test view
    return JsonResponse({})


class DjangoPluginTestCase(unittest.TestCase):
    def setUp(self):
        self.plugin = DjangoPlugin()
        self.rf = RequestFactory()
        self.config = Configuration()
        self.url = url(r'test', view, name='test_view')

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

        payload = self.plugin.generate_payload(self.config, {'foo': 'bar'})
        self.assertEqual(payload['url'], 'http://testserver/test?a=1')
        self.assertEqual(payload['action'], 'view')
        self.assertDictEqual(payload['params'], {'a': ['1']})
        self.assertDictEqual(payload['session'], {})
        self.assertDictEqual(payload['context'], {'foo': 'bar'})

    def test_generate_payload_post(self):
        request = self.rf.post('test', data={'a': 1, 'b': 2, 'password': 'notsafe'})
        request.resolver_match = self.url.resolve('test')
        set_request(request)

        payload = self.plugin.generate_payload(self.config, {'foo': 'bar'})
        self.assertEqual(payload['url'], 'http://testserver/test')
        self.assertEqual(payload['action'], 'view')
        self.assertDictEqual(payload['params'], {'a': ['1'], 'b': ['2'], 'password': '[FILTERED]'})
        self.assertDictEqual(payload['session'], {})
        self.assertDictEqual(payload['context'], {'foo': 'bar'})

    def test_generate_payload_with_session(self):
        request = self.rf.get('test')
        request.resolver_match = self.url.resolve('test')
        request.session = {'lang': 'en'}
        set_request(request)

        payload = self.plugin.generate_payload(self.config, {'foo': 'bar'})
        self.assertEqual(payload['url'], 'http://testserver/test')
        self.assertEqual(payload['action'], 'view')
        self.assertDictEqual(payload['session'], {'lang': 'en'})
        self.assertDictEqual(payload['context'], {'foo': 'bar'})
