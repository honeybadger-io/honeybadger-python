import unittest
from unittest.mock import patch
from celery import Celery
from honeybadger import honeybadger
from honeybadger.contrib.celery import CeleryHoneybadger

import honeybadger.connection as connection


class CeleryPluginTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = Celery(__name__)
        self.app.conf.update(
            CELERY_ALWAYS_EAGER=True,
            HONEYBADGER_API_KEY="test",
            HONEYBADGER_ENVIRONMENT="celery_test",
            HONEYBADGER_FORCE_REPORT_DATA=True,
        )
        self.celery_hb = None

    def get_mock_notice_payload(self, mock):
        return mock.call_args[0][1].payload

    def tearDown(self):
        super().tearDown()
        if self.celery_hb:
            self.celery_hb.tearDown()

    @patch("honeybadger.connection._make_http_request")
    @patch("honeybadger.connection.send_notice", wraps=connection.send_notice)
    def test_celery_task_with_exception(self, mock, mock_request):
        self.celery_hb = CeleryHoneybadger(self.app, report_exceptions=True)

        @self.app.task
        def error():
            return 1 / 0

        error.delay()
        mock.assert_called_once()
        self.assertEqual(
            self.get_mock_notice_payload(mock)["error"]["class"], "ZeroDivisionError"
        )
        self.assertEqual(
            self.get_mock_notice_payload(mock)["error"]["message"], "division by zero"
        )

    @patch("honeybadger.connection._make_http_request")
    @patch("honeybadger.connection.send_notice", wraps=connection.send_notice)
    def test_celery_task_with_params(self, mock, mock_request):
        self.celery_hb = CeleryHoneybadger(self.app, report_exceptions=True)

        @self.app.task
        def error(a, b, c):
            return a / b

        error.delay(1, 0, c=3)
        mock.assert_called_once()
        self.assertEqual(
            self.get_mock_notice_payload(mock)["request"]["params"]["args"], [1, 0]
        )
        self.assertEqual(
            self.get_mock_notice_payload(mock)["request"]["params"]["kwargs"], {"c": 3}
        )

    @patch("honeybadger.connection._make_http_request")
    @patch("honeybadger.connection.send_notice", wraps=connection.send_notice)
    def test_celery_task_without_retries(self, mock, mock_request):
        self.celery_hb = CeleryHoneybadger(self.app, report_exceptions=True)

        @self.app.task
        def error():
            return 1 / 0

        error.delay()
        mock.assert_called_once()
        self.assertEqual(
            self.get_mock_notice_payload(mock)["request"]["context"]["retries"], 0
        )
        self.assertEqual(
            self.get_mock_notice_payload(mock)["request"]["context"]["max_retries"], 3
        )

    @patch("honeybadger.connection._make_http_request")
    @patch("honeybadger.connection.send_notice", wraps=connection.send_notice)
    def test_celery_task_with_retries(self, mock, mock_request):
        self.celery_hb = CeleryHoneybadger(self.app, report_exceptions=True)

        @self.app.task(bind=True, max_retries=5, autoretry_for=(ZeroDivisionError,))
        def error(self):
            return 1 / 0

        error.delay()
        mock.assert_called_once()
        self.assertEqual(
            self.get_mock_notice_payload(mock)["request"]["context"]["retries"], 5
        )
        self.assertEqual(
            self.get_mock_notice_payload(mock)["request"]["context"]["max_retries"], 5
        )

    @patch("honeybadger.connection._make_http_request")
    @patch("honeybadger.connection.send_notice", wraps=connection.send_notice)
    @patch("honeybadger.honeybadger.reset_context")
    def test_celery_task_with_reset_context(self, mock_reset, mock_send, mock_request):
        self.celery_hb = CeleryHoneybadger(self.app, report_exceptions=True)

        @self.app.task
        def error():
            return 1 / 0

        error.delay()
        mock_send.assert_called_once()
        mock_reset.assert_called_once()

    @patch("honeybadger.connection._make_http_request")
    @patch("honeybadger.connection.send_notice", wraps=connection.send_notice)
    def test_without_auto_report_exceptions(self, mock, mock_request):
        self.celery_hb = CeleryHoneybadger(self.app, report_exceptions=False)

        @self.app.task
        def error():
            return 1 / 0

        error.delay()
        mock.assert_not_called()

    @patch("honeybadger.connection._make_http_request")
    @patch("honeybadger.connection.send_notice", wraps=connection.send_notice)
    def test_context_merging(self, mock, mock_request):
        """Test that custom context is merged with task context rather than being replaced"""
        self.celery_hb = CeleryHoneybadger(self.app, report_exceptions=True)

        @self.app.task
        def error():
            with honeybadger.context(user_id=123, custom_data="test"):
                return 1 / 0

        error.delay()
        mock.assert_called_once()

        # Verify task context is present
        context = self.get_mock_notice_payload(mock)["request"]["context"]
        self.assertIn("task_id", context)
        self.assertIn("retries", context)
        self.assertIn("max_retries", context)

        # Verify custom context is also present
        self.assertEqual(context["user_id"], 123)
        self.assertEqual(context["custom_data"], "test")
