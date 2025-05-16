import unittest
import importlib

import sys
from mock import patch

from honeybadger import honeybadger
from honeybadger.config import Configuration
from honeybadger.contrib.flask import FlaskPlugin, FlaskHoneybadger

PYTHON_VERSION = sys.version_info[0:2]


class FlaskPluginTestCase(unittest.TestCase):
    def setUp(self):
        import flask

        if importlib.metadata.version("flask").startswith("0.12") and PYTHON_VERSION < (
            3,
            3,
        ):
            self.skipTest("Flask 0.12 requires Python > 3.2")

        if importlib.metadata.version("flask").startswith("1.0") and PYTHON_VERSION < (
            3,
            4,
        ):
            self.skipTest("Flask 1.0 requires Python > 3.3")

        if importlib.metadata.version("flask").startswith("1.1") and PYTHON_VERSION < (
            3,
            5,
        ):
            self.skipTest("Flask 1.1 requires Python > 3.4")

        self.config = Configuration()
        self.default_payload = {"request": {}}

        self.app = flask.Flask(__name__)
        self.app.secret_key = "safe"
        self.app.config.update({"HONEYBADGER_ENVIRONMENT": "production_flask"})
        self.plugin = FlaskPlugin()

        @self.app.route("/test", methods=["GET", "POST", "PUT"])
        def foo():
            return "bar"

    def test_supports_in_request_context(self):
        with self.app.test_request_context():
            self.assertTrue(self.plugin.supports(self.config, {}))

    def test_supports_no_request_context(self):
        self.assertFalse(self.plugin.supports(self.config, {}))

    def test_get_request_with_headers(self):
        with self.app.test_request_context(
            path="/test",
            base_url="http://server:1234/path",
            query_string="a=1&a=2&foo=bar",
            headers={"X-Wizard-Color": "grey"},
        ):
            payload = self.plugin.generate_payload(
                self.default_payload, self.config, {"k": "value"}
            )

            self.assertEqual(payload["request"]["url"], "http://server:1234/path/test")
            self.assertEqual(
                payload["request"]["component"], "honeybadger.tests.contrib.test_flask"
            )
            self.assertEqual(payload["request"]["action"], "foo")
            self.assertDictEqual(
                payload["request"]["params"], {"a": ["1", "2"], "foo": ["bar"]}
            )
            self.assertDictEqual(payload["request"]["session"], {})
            self.assertEqual(
                payload["request"]["cgi_data"],
                {
                    **payload["request"]["cgi_data"],
                    **{"Host": "server:1234", "REQUEST_METHOD": "GET"},
                },
            )
            self.assertDictEqual(payload["request"]["context"], {"k": "value"})

    def test_get_request_with_session(self):
        with self.app.test_request_context(
            path="/test", base_url="http://server:1234/path"
        ) as ctx:
            ctx.session["answer"] = 42
            ctx.session["password"] = "this is fine"

            payload = self.plugin.generate_payload(
                self.default_payload, self.config, {"k": "value"}
            )

            self.assertEqual(payload["request"]["url"], "http://server:1234/path/test")
            self.assertEqual(
                payload["request"]["component"], "honeybadger.tests.contrib.test_flask"
            )
            self.assertEqual(payload["request"]["action"], "foo")
            self.assertDictEqual(payload["request"]["params"], {})
            self.assertDictEqual(
                payload["request"]["session"], {"answer": 42, "password": "[FILTERED]"}
            )
            self.assertEqual(
                payload["request"]["cgi_data"],
                {
                    **payload["request"]["cgi_data"],
                    **{"Host": "server:1234", "REQUEST_METHOD": "GET"},
                },
            )
            self.assertDictEqual(payload["request"]["context"], {"k": "value"})

    def test_post_request(self):
        with self.app.test_request_context(
            path="/test",
            base_url="http://server:1234/path",
            method="POST",
            data={"foo": "bar", "password": "this is file"},
        ):

            payload = self.plugin.generate_payload(
                self.default_payload, self.config, {"k": "value"}
            )

            self.assertEqual(payload["request"]["url"], "http://server:1234/path/test")
            self.assertEqual(
                payload["request"]["component"], "honeybadger.tests.contrib.test_flask"
            )
            self.assertEqual(payload["request"]["action"], "foo")
            self.assertDictEqual(
                payload["request"]["params"], {"foo": ["bar"], "password": "[FILTERED]"}
            )
            self.assertDictEqual(payload["request"]["session"], {})
            self.assertDictEqual(
                payload["request"]["cgi_data"],
                {
                    "Host": "server:1234",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": "29",
                    "REQUEST_METHOD": "POST",
                    "HTTP_COOKIE": {},
                },
            )
            self.assertDictEqual(payload["request"]["context"], {"k": "value"})

    def test_put_request(self):
        with self.app.test_request_context(
            path="/test",
            base_url="http://server:1234/path",
            method="PUT",
            data={"foo": "bar", "password": "this is file"},
        ):

            payload = self.plugin.generate_payload(
                self.default_payload, self.config, {"k": "value"}
            )

            self.assertEqual(payload["request"]["url"], "http://server:1234/path/test")
            self.assertEqual(
                payload["request"]["component"], "honeybadger.tests.contrib.test_flask"
            )
            self.assertEqual(payload["request"]["action"], "foo")
            self.assertDictEqual(
                payload["request"]["params"], {"foo": ["bar"], "password": "[FILTERED]"}
            )
            self.assertDictEqual(payload["request"]["session"], {})
            self.assertDictEqual(
                payload["request"]["cgi_data"],
                {
                    "Host": "server:1234",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": "29",
                    "REQUEST_METHOD": "PUT",
                    "HTTP_COOKIE": {},
                },
            )
            self.assertDictEqual(payload["request"]["context"], {"k": "value"})


class FlaskHoneybadgerTestCase(unittest.TestCase):

    def setUp(self):
        import flask

        if importlib.metadata.version("flask").startswith("0.12") and PYTHON_VERSION < (
            3,
            3,
        ):
            self.skipTest("Flask 0.12 requires Python > 3.2")

        if importlib.metadata.version("flask").startswith("1.0") and PYTHON_VERSION < (
            3,
            4,
        ):
            self.skipTest("Flask 1.0 requires Python >= 3.4")

        if importlib.metadata.version("flask").startswith("1.1") and PYTHON_VERSION < (
            3,
            5,
        ):
            self.skipTest("Flask 1.1 requires Python >= 3.5")

        self.default_headers = {
            "Content-Length": "0",
            "Host": "localhost",
            "User-Agent": "Honeybadger Test",
        }
        self.app = flask.Flask(__name__)
        self.app.config.update({"environment": "production_flask", "api_key": "abcd"})

    def assert_called_with_exception_type(self, mock_hb, exc_type):
        self.assertEqual(1, mock_hb.notify.call_count)
        self.assertEqual(exc_type, type(mock_hb.notify.call_args[0][0]))

    @patch("honeybadger.contrib.flask.honeybadger")
    def test_with_auto_report_exceptions(self, mock_hb):
        FlaskHoneybadger(self.app, report_exceptions=True)

        @self.app.route("/error")
        def error():
            return 1 / 0

        self.app.test_client().get("/error?a=1&b=2&b=3")

        self.assert_called_with_exception_type(mock_hb, ZeroDivisionError)

    @patch("honeybadger.contrib.flask.honeybadger")
    def test_without_auto_report_exceptions(self, mock_hb):
        FlaskHoneybadger(self.app, report_exceptions=False)

        @self.app.route("/error")
        def error():
            return 1 / 0

        self.app.test_client().get("/error?a=1&b=2&b=3")

        mock_hb.notify.assert_not_called()

    @patch("honeybadger.contrib.flask.honeybadger")
    def test_auto_report_exceptions_with_blueprint(self, mock_hb):
        from flask import Blueprint

        FlaskHoneybadger(self.app, report_exceptions=True)

        bp = Blueprint("blueprint", __name__)

        @bp.route("/error")
        def error():
            return 1 / 0

        self.app.register_blueprint(bp)

        self.app.test_client().get("/error?a=1&b=2&b=3")

        self.assert_called_with_exception_type(mock_hb, ZeroDivisionError)

    @patch("honeybadger.contrib.flask.honeybadger")
    def test_auto_report_exceptions_with_view_class(self, mock_hb):
        from flask.views import MethodView

        FlaskHoneybadger(self.app, report_exceptions=True)

        class ErrorView(MethodView):
            def get(self):
                return 1 / 0

        self.app.add_url_rule("/error", view_func=ErrorView.as_view("error"))

        self.app.test_client().get("/error?a=1&b=2&b=3")

        self.assert_called_with_exception_type(mock_hb, ZeroDivisionError)

    @patch("honeybadger.contrib.flask.honeybadger")
    def test_dont_reset_context_with_exception(self, mock_hb):
        from flask.views import MethodView

        FlaskHoneybadger(
            self.app, report_exceptions=True, reset_context_after_request=False
        )

        honeybadger.set_context(foo="bar")

        class ErrorView(MethodView):
            def get(self):
                return 1 / 0

        self.app.add_url_rule("/error", view_func=ErrorView.as_view("error"))

        self.app.test_client().get("/error?a=1&b=2&b=3")

        self.assert_called_with_exception_type(mock_hb, ZeroDivisionError)
        mock_hb.reset_context.assert_not_called()

    @patch("honeybadger.contrib.flask.honeybadger")
    def test_dont_reset_context_when_not_reporting(self, mock_hb):
        from flask.views import MethodView

        FlaskHoneybadger(
            self.app, report_exceptions=False, reset_context_after_request=False
        )

        honeybadger.set_context(foo="bar")

        class ErrorView(MethodView):
            def get(self):
                return 1 / 0

        self.app.add_url_rule("/error", view_func=ErrorView.as_view("error"))

        self.app.test_client().get("/error?a=1&b=2&b=3")

        mock_hb.notify.assert_not_called()
        mock_hb.reset_context.assert_not_called()

    @patch("honeybadger.contrib.flask.honeybadger")
    def test_reset_context_when_not_reporting(self, mock_hb):
        from flask.views import MethodView

        FlaskHoneybadger(
            self.app, report_exceptions=False, reset_context_after_request=True
        )

        honeybadger.set_context(foo="bar")

        class ErrorView(MethodView):
            def get(self):
                return 1 / 0

        self.app.add_url_rule("/error", view_func=ErrorView.as_view("error"))

        self.app.test_client().get("/error?a=1&b=2&b=3")

        mock_hb.notify.assert_not_called()
        self.assertEqual(1, mock_hb.reset_context.call_count)

    @patch("honeybadger.contrib.flask.honeybadger")
    def test_reset_context_when_reporting(self, mock_hb):
        from flask.views import MethodView

        FlaskHoneybadger(
            self.app, report_exceptions=True, reset_context_after_request=True
        )

        honeybadger.set_context(foo="bar")

        class ErrorView(MethodView):
            def get(self):
                return 1 / 0

        self.app.add_url_rule("/error", view_func=ErrorView.as_view("error"))

        self.app.test_client().get("/error?a=1&b=2&b=3")

        self.assert_called_with_exception_type(mock_hb, ZeroDivisionError)
        self.assertEqual(2, mock_hb.reset_context.call_count)


class FlaskHoneybadgerInsightsTestCase(unittest.TestCase):
    def setUp(self):
        import flask

        self.app = flask.Flask(__name__)
        # minimal honeybadger config
        self.app.config.update(
            {
                "HONEYBADGER_INSIGHTS_ENABLED": True,
                "HONEYBADGER_API_KEY": "test",
                "HONEYBADGER_ENVIRONMENT": "test",
            }
        )
        # install the extension (hooks get registered here)
        FlaskHoneybadger(self.app)

        # a simple endpoint to drive requests
        @self.app.route("/ping", methods=["GET", "POST"])
        def ping():
            return "pong", 201

        self.client = self.app.test_client()

    @patch("honeybadger.contrib.flask.honeybadger.event")
    def test_insights_event_on_get(self, mock_event):
        resp = self.client.get("/ping?foo=bar")
        self.assertEqual(resp.status_code, 201)
        # should fire exactly once per request
        self.assertEqual(mock_event.call_count, 1)

        name, payload = mock_event.call_args[0]
        self.assertEqual(name, "flask.request")
        self.assertEqual(payload["method"], "GET")
        self.assertEqual(payload["path"], "/ping")
        self.assertEqual(payload["status"], 201)
        self.assertEqual(payload["view"], "ping")
        self.assertTrue(isinstance(payload["duration"], float))

    @patch("honeybadger.contrib.flask.honeybadger.event")
    def test_insights_event_on_post(self, mock_event):
        resp = self.client.post("/ping", data={"a": "1", "b": "2"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(mock_event.call_count, 1)

        _, payload = mock_event.call_args[0]
        self.assertEqual(payload["method"], "POST")
        self.assertEqual(payload["path"], "/ping")
        # query‐string not present → still only path
        self.assertEqual(payload["view"], "ping")
        # duration should be non‐negative
        self.assertGreaterEqual(payload["duration"], 0.0)
