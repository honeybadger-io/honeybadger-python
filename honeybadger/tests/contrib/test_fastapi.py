import unittest
import aiounittest
import mock
from honeybadger.contrib.fastapi import HoneybadgerRoute
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FastAPITestCase(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.router.route_class = HoneybadgerRoute

        @app.get("/ok")
        def ok_route():
            return "ok"

        @app.get("/ko")
        def ko_route():
            return 1 / 0

        self.client = TestClient(app, raise_server_exceptions=False)

    @mock.patch("honeybadger.contrib.fastapi.honeybadger")
    def test_should_not_notify_on_ok_route(self, hb):
        response = self.client.get("/ok")
        self.assertEqual(response.status_code, 200)
        hb.notify.assert_not_called()

    @mock.patch("honeybadger.contrib.fastapi.honeybadger")
    def test_should_notify_on_ko_route(self, hb):
        response = self.client.get("/ko")
        self.assertEqual(response.status_code, 500)
        hb.notify.assert_called_once()
        self.assertEqual(type(hb.notify.call_args.kwargs["exception"]), ZeroDivisionError)