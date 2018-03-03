import unittest

from django.conf.urls import url
from mock import patch, Mock

from django.http import JsonResponse
from django.test import RequestFactory

from honeybadger import honeybadger
from honeybadger.contrib.django import current_request, clear_request
from honeybadger.middleware import DjangoHoneybadgerMiddleware


def view(request):
    # Test view
    return JsonResponse({})


class MiddlewareTestCase(unittest.TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.url = url(r'test', view, name='test_view')
        self.middleware = DjangoHoneybadgerMiddleware()

    def tearDown(self):
        clear_request()

    def test_process_request(self):
        request = self.rf.get('test')
        self.middleware.process_request(request)
        self.assertEqual(request, current_request())

    @patch('honeybadger.middleware.honeybadger')
    def test_process_exception(self, mock_hb):
        request = self.rf.get('test')
        self.middleware.process_request(request)
        exc = ValueError('test exception')
        self.middleware.process_exception(request, exc)

        mock_hb.notify.assert_called_with(exc)
        self.assertIsNone(current_request(), msg='Current request should be cleared after exception handling')

    def test_process_response(self):
        request = self.rf.get('test')
        response = Mock()
        honeybadger.set_context(foo='bar')
        self.middleware.process_request(request)

        self.middleware.process_response(request, response)
        self.assertDictEqual({}, honeybadger._get_context(), msg='Context should be cleared after response handling')
        self.assertIsNone(current_request(), msg='Current request should be cleared after response handling')
