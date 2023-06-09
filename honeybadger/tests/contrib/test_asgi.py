import pprint
import unittest
from async_asgi_testclient import TestClient
import aiounittest
import mock
from honeybadger import contrib


class SomeError(Exception):
    pass


def asgi_app():
    """Example ASGI App."""
    async def app(scope, receive, send):
        if "error" in scope["path"]:
            raise SomeError("Some Error.")
        headers = [(b"content-type", b"text/html")]
        body = f"<pre>{pprint.PrettyPrinter(indent=2, width=256).pformat(scope)}</pre>".encode("utf-8")
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
