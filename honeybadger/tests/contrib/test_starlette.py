import unittest
import mock

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from honeybadger.contrib.starlette import StarletteHoneybadger


class SomeError(Exception):
    pass


def ok_route(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def error_route(request: Request) -> PlainTextResponse:
    raise SomeError("Something went wrong")


def build_app(**kwargs):
    app = Starlette(
        routes=[
            Route("/ok", ok_route, name="ok"),
            Route("/error", error_route, name="error"),
        ],
    )
    app.add_middleware(StarletteHoneybadger, **kwargs)
    return app


class StarletteMiddlewareTestCase(unittest.TestCase):
    def setUp(self):
        self.hb_patcher = mock.patch("honeybadger.contrib.starlette.honeybadger")
        self.hb = self.hb_patcher.start()
        self.app = build_app(api_key="test-key")
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.hb_patcher.stop()

    def test_should_not_notify_on_ok_route(self):
        response = self.client.get("/ok")
        self.assertEqual(response.status_code, 200)
        self.hb.notify.assert_not_called()

    def test_should_notify_on_error_route(self):
        response = self.client.get("/error")
        self.assertEqual(response.status_code, 500)
        self.hb.notify.assert_called_once()
        self.assertEqual(
            type(self.hb.notify.call_args.kwargs["exception"]), SomeError
        )

    def test_should_begin_request(self):
        self.client.get("/ok")
        self.hb.begin_request.assert_called_once()
        # Verify begin_request was called with the request object
        args = self.hb.begin_request.call_args.args
        self.assertEqual(len(args), 1)

    def test_should_reset_context(self):
        self.hb.config.insights_enabled = False
        self.client.get("/ok")
        self.hb.reset_context.assert_called()

    def test_should_set_event_context_with_request_id(self):
        self.hb.config.insights_enabled = False
        self.client.get("/ok")
        self.hb.set_event_context.assert_called_once()
        call_kwargs = self.hb.set_event_context.call_args.kwargs
        self.assertIn("request_id", call_kwargs)

    def test_should_use_provided_request_id(self):
        self.hb.config.insights_enabled = False
        self.client.get("/ok", headers={"x-request-id": "my-request-id"})
        self.hb.set_event_context.assert_called_once()
        call_kwargs = self.hb.set_event_context.call_args.kwargs
        self.assertEqual(call_kwargs["request_id"], "my-request-id")


class StarletteInsightsTestCase(unittest.TestCase):
    @mock.patch("honeybadger.contrib.starlette.honeybadger")
    def test_sends_request_event(self, hb):
        hb.config.insights_enabled = True
        hb.config.insights_config.starlette.disabled = False
        hb.config.insights_config.starlette.include_params = False
        hb.config.params_filters = ["password"]

        app = build_app(api_key="test-key")
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/ok?x=1")

        hb.event.assert_called_once()
        name, payload = hb.event.call_args.args
        self.assertEqual(name, "starlette.request")
        self.assertEqual(payload["method"], "GET")
        self.assertEqual(payload["path"], "/ok")
        self.assertEqual(payload["status"], 200)
        self.assertIsInstance(payload["duration"], float)

    @mock.patch("honeybadger.contrib.starlette.honeybadger")
    def test_disabled_insights(self, hb):
        hb.config.insights_enabled = True
        hb.config.insights_config.starlette.disabled = True

        app = build_app(api_key="test-key")
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/ok")

        hb.event.assert_not_called()

    @mock.patch("honeybadger.contrib.starlette.honeybadger")
    def test_insights_with_params(self, hb):
        hb.config.insights_enabled = True
        hb.config.insights_config.starlette.disabled = False
        hb.config.insights_config.starlette.include_params = True
        hb.config.params_filters = ["password"]

        app = build_app(api_key="test-key")
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/ok?x=1&password=secret&y=2&y=3")

        hb.event.assert_called_once()
        name, payload = hb.event.call_args.args
        self.assertEqual(payload["params"], {"x": "1", "y": ["2", "3"]})

    @mock.patch("honeybadger.contrib.starlette.honeybadger")
    def test_includes_route_info(self, hb):
        hb.config.insights_enabled = True
        hb.config.insights_config.starlette.disabled = False
        hb.config.insights_config.starlette.include_params = False
        hb.config.params_filters = []

        app = build_app(api_key="test-key")
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/ok")

        hb.event.assert_called_once()
        name, payload = hb.event.call_args.args
        self.assertEqual(payload["route"], "/ok")
        self.assertEqual(payload["view"], "ok")


class StarlettePluginTestCase(unittest.TestCase):
    def test_supports_with_starlette_request(self):
        from honeybadger.contrib.starlette import StarlettePlugin

        plugin = StarlettePlugin()
        self.assertTrue(plugin.supports(None, {"starlette_request": "something"}))

    def test_does_not_support_without_starlette_request(self):
        from honeybadger.contrib.starlette import StarlettePlugin

        plugin = StarlettePlugin()
        self.assertFalse(plugin.supports(None, {}))

    def test_generate_payload_includes_basic_request_data(self):
        from honeybadger.contrib.starlette import StarlettePlugin

        scope = {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/ok",
            "root_path": "",
            "query_string": b"x=1&x=2",
            "headers": [
                (b"host", b"testserver"),
                (b"user-agent", b"test-client"),
            ],
        }

        async def receive():
            return {"type": "http.request"}

        request = Request(scope, receive)

        plugin = StarlettePlugin()
        payload = {"request": {}}
        config = mock.Mock()
        config.params_filters = []
        context = {"starlette_request": request}

        plugin.generate_payload(payload, config, context)

        self.assertIn("request", payload)
        request_payload = payload["request"]

        self.assertEqual(request_payload.get("method"), "GET")
        self.assertEqual(request_payload.get("path"), "/ok")

        params = request_payload.get("params", {})
        self.assertIn("x", params)
        x_value = params["x"]
        if isinstance(x_value, (list, tuple)):
            self.assertEqual(list(x_value), ["1", "2"])
        else:
            self.assertIn("1", str(x_value))
            self.assertIn("2", str(x_value))

    def test_generate_payload_filters_sensitive_params(self):
        from honeybadger.contrib.starlette import StarlettePlugin

        scope = {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/ok",
            "root_path": "",
            "query_string": b"password=secret&x=1",
            "headers": [
                (b"host", b"testserver"),
            ],
        }

        async def receive():
            return {"type": "http.request"}

        request = Request(scope, receive)

        plugin = StarlettePlugin()
        payload = {"request": {}}
        config = mock.Mock()
        config.params_filters = ["password"]
        context = {"starlette_request": request}

        plugin.generate_payload(payload, config, context)

        self.assertIn("request", payload)
        request_payload = payload["request"]
        params = request_payload.get("params", {})

        if "password" in params:
            self.assertNotEqual(params["password"], "secret")
        self.assertIn("x", params)
