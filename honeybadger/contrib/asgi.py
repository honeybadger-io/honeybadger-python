from honeybadger import honeybadger, plugins, utils
from honeybadger.utils import get_duration
import logging
import time
import urllib
import inspect
import asyncio
import json
from typing import Dict, Any, Optional, Callable, Awaitable, Union, Tuple, List, cast

logger = logging.getLogger(__name__)


def _looks_like_asgi3(app) -> bool:
    # https://github.com/encode/uvicorn/blob/bf1c64e2c141971c546671c7dc91b8ccf0afeb7d/uvicorn/config.py#L327
    if inspect.isclass(app):
        return hasattr(app, "__await__")
    elif inspect.isfunction(app):
        return asyncio.iscoroutinefunction(app)
    else:
        call = getattr(app, "__call__", None)
        return asyncio.iscoroutinefunction(call)
    return False


def _get_headers(scope: dict) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for raw_key, raw_value in scope["headers"]:
        key = raw_key.decode("latin-1")
        value = raw_value.decode("latin-1")
        if key in headers:
            headers[key] = headers[key] + ", " + value
        else:
            headers[key] = value
    return headers


def _get_query(scope: dict) -> Optional[str]:
    qs = scope.get("query_string")
    if not qs:
        return None
    return urllib.parse.unquote(qs.decode("latin-1"))


def _get_url(scope: dict, default_scheme: str, host: Optional[str] = None) -> str:
    scheme = scope.get("scheme", default_scheme)
    server = scope.get("server")
    path = scope.get("root_path", "") + scope.get("path", "")
    if host:
        return "%s://%s%s" % (scheme, host, path)

    if server is not None:
        host, port = server
        default_port = {"http": 80, "https": 443, "ws": 80, "wss": 443}[scheme]
        if port != default_port:
            return "%s://%s:%s%s" % (scheme, host, port, path)
        return "%s://%s%s" % (scheme, host, path)
    return path


def _get_body(scope: dict) -> Optional[Union[Dict[Any, Any], str]]:
    body = scope.get("body")
    if body is None:
        return None

    try:
        return json.loads(body)
    except:
        return urllib.parse.unquote(body.decode("latin-1"))


def _as_context(scope: dict) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    if scope.get("type") in ("http", "websocket"):
        ctx["method"] = scope.get("method")
        ctx["headers"] = headers = _get_headers(scope)
        ctx["query_string"] = _get_query(scope)
        host_header = headers.get("host")
        ctx["url"] = _get_url(
            scope, "http" if scope["type"] == "http" else "ws", host_header
        )
        body = _get_body(scope)
        if body is not None:
            ctx["body"] = body

    ctx["client"] = scope.get("client")  # pii info can be filtered from hb config.

    # TODO: should we look at "endpoint"?
    return utils.filter_dict(ctx, honeybadger.config.params_filters)


class ASGIHoneybadger(plugins.Plugin):
    __slots__ = ("__call__", "app")

    def __init__(self, app, **kwargs):
        super().__init__("ASGI")

        if kwargs:
            honeybadger.configure(**kwargs)

        self.app = app

        if _looks_like_asgi3(app):
            self.__call__ = self._run_asgi3
        else:
            self.__call__ = self._run_asgi2

        plugins.default_plugin_manager.register(self)

    def _run_asgi2(self, scope):
        async def inner(receive, send):
            return await self._run_request(scope, receive, send, self.app(scope))

        return inner

    async def _run_asgi3(self, scope, receive, send):
        return await self._run_request(
            scope, receive, send, lambda recv, snd: self.app(scope, recv, snd)
        )

    async def _run_request(self, scope, receive, send, app_callable):
        # TODO: Should we check recursive middleware stacks?
        # See: https://github.com/getsentry/sentry-python/blob/master/sentry_sdk/integrations/asgi.py#L112
        start = time.time()
        status = None

        async def send_wrapper(message):
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message.get("status")
            await send(message)

        try:
            return await app_callable(receive, send_wrapper)
        except Exception as exc:
            honeybadger.notify(exception=exc, context=_as_context(scope))
            raise
        finally:
            try:
                asgi_config = honeybadger.config.insights_config.asgi
                if honeybadger.config.insights_enabled and not asgi_config.disabled:
                    payload = {
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status,
                        "duration": get_duration(start),
                    }

                    if asgi_config.include_params:
                        raw_qs = scope.get("query_string", b"")
                        params = {}
                        if raw_qs:
                            parsed = urllib.parse.parse_qs(raw_qs.decode())
                            for key, values in parsed.items():
                                params[key] = values[0] if len(values) == 1 else values

                        payload["params"] = utils.filter_dict(
                            params,
                            honeybadger.config.params_filters,
                            remove_keys=True,
                        )

                    honeybadger.event("asgi.request", payload)
                honeybadger.reset_context()
            except Exception as e:
                logger.warning(
                    f"Exception while sending Honeybadger event: {e}", exc_info=True
                )

    def supports(self, config, context):
        return context.get("asgi") is not None

    def generate_payload(self, default_payload, config, context):
        return utils.filter_dict(default_payload, honeybadger.config.params_filters)
