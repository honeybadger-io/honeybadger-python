import pprint
import unittest
from async_asgi_testclient import TestClient  # type: ignore
import aiounittest
import mock

from honeybadger import honeybadger
from honeybadger import contrib
from honeybadger.config import Configuration
from honeybadger.tests.utils import with_config


class SomeError(Exception):
    pass


def asgi_app():
    """Example ASGI App."""

    async def app(scope, receive, send):
        if "error" in scope["path"]:
            raise SomeError("Some Error.")
        headers = [(b"content-type", b"text/html")]
        body = f"<pre>{pprint.PrettyPrinter(indent=2, width=256).pformat(scope)}</pre>".encode(
            "utf-8"
        )
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return app


class ASGIPluginTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(contrib.ASGIHoneybadger(asgi_app(), api_key="abcd"))

    @mock.patch("honeybadger.contrib.asgi.honeybadger")
    def test_should_support_asgi(self, hb):
        asgi_context = {"asgi": {"version": "3.0"}}
        non_asgi_context = {}
        self.assertTrue(self.client.application.supports(hb.config, asgi_context))
        self.assertFalse(self.client.application.supports(hb.config, non_asgi_context))

    @aiounittest.async_test
    @mock.patch("honeybadger.contrib.asgi.honeybadger")
    async def test_should_notify_exception(self, hb):
        with self.assertRaises(SomeError):
            await self.client.get("/error")
        hb.notify.assert_called_once()
        self.assertEqual(type(hb.notify.call_args.kwargs["exception"]), SomeError)

    @aiounittest.async_test
    @mock.patch("honeybadger.contrib.asgi.honeybadger")
    async def test_should_not_notify_exception(self, hb):
        response = await self.client.get("/")
        hb.notify.assert_not_called()


class ASGIEventPayloadTestCase(unittest.TestCase):
    @aiounittest.async_test
    @with_config({"insights_enabled": True})
    @mock.patch("honeybadger.contrib.asgi.honeybadger.event")
    async def test_success_event_payload(self, event):
        app = TestClient(contrib.ASGIHoneybadger(asgi_app(), api_key="abcd"))
        # even if there’s a query, url stays just the path
        await app.get("/hello?x=1")
        event.assert_called_once()
        name, payload = event.call_args.args

        self.assertEqual(name, "asgi.request")
        self.assertEqual(payload["method"], "GET")
        self.assertEqual(payload["path"], "/hello")
        self.assertEqual(payload["status"], 200)
        self.assertIsInstance(payload["duration"], float)

    @aiounittest.async_test
    @with_config(
        {"insights_enabled": True, "insights_config": {"asgi": {"disabled": True}}}
    )
    @mock.patch("honeybadger.contrib.asgi.honeybadger.event")
    async def test_disabled_by_insights_config(self, event):
        app = TestClient(contrib.ASGIHoneybadger(asgi_app(), api_key="abcd"))
        await app.get("/hello?x=1")
        event.assert_not_called()

    @aiounittest.async_test
    @with_config(
        {
            "insights_enabled": True,
            "insights_config": {"asgi": {"include_params": True}},
        }
    )
    @mock.patch("honeybadger.contrib.asgi.honeybadger.event")
    async def test_disable_insights(self, event):
        app = TestClient(
            contrib.ASGIHoneybadger(asgi_app(), api_key="abcd", insights_enabled=True)
        )
        await app.get("/hello?x=1&password=secret&y=2&y=3")
        event.assert_called_once()
        name, payload = event.call_args.args
        self.assertEqual(payload["params"], {"x": "1", "y": ["2", "3"]})
