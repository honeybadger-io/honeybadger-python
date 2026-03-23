import logging
import time
import uuid
from contextvars import ContextVar
from typing import Optional

from honeybadger import honeybadger
from honeybadger.plugins import Plugin, default_plugin_manager
from honeybadger.utils import (
    filter_dict,
    filter_env_vars,
    get_duration,
    sanitize_request_id,
)
from honeybadger.contrib.asgi import _as_context

from starlette.types import ASGIApp
from starlette.requests import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.routing import Match

logger = logging.getLogger(__name__)

_current_request: ContextVar[Optional[Request]] = ContextVar(
    "_current_request", default=None
)

# CGI-style header keys that carry credentials and are always stripped
# from error payloads.
_SENSITIVE_CGI_HEADERS = frozenset(
    {
        "HTTP_AUTHORIZATION",
        "HTTP_PROXY_AUTHORIZATION",
        "HTTP_COOKIE",
    }
)


class StarlettePlugin(Plugin):
    """Plugin to extract Starlette request data for error payloads."""

    def __init__(self):
        super().__init__("Starlette")

    def supports(self, config, context):
        request = context.get("starlette_request") or _current_request.get()
        return request is not None

    def generate_payload(self, default_payload, config, context):
        request = context.get("starlette_request") or _current_request.get()
        if request is None:
            return default_payload

        route, route_name = _match_route(request)

        cgi_data = {}
        for key, value in request.headers.items():
            cgi_key = "HTTP_" + key.upper().replace("-", "_")
            if cgi_key not in _SENSITIVE_CGI_HEADERS:
                cgi_data[cgi_key] = value
        cgi_data["REQUEST_METHOD"] = request.method

        params = {}
        for key in request.query_params:
            values = request.query_params.getlist(key)
            params[key] = values if len(values) > 1 else values[0] if values else None

        payload = {
            "url": str(request.url.replace(query=None)),
            "component": route or request.url.path,
            "action": route_name or request.method,
            "params": filter_dict(params, config.params_filters),
            "cgi_data": filter_dict(filter_env_vars(cgi_data), config.params_filters),
            "context": {k: v for k, v in context.items() if k != "starlette_request"},
            "method": request.method,
            "path": request.url.path,
        }

        default_payload["request"].update(payload)
        return default_payload


def _match_route(request: Request):
    """Try to match the request to a route and return (route_path, route_name)."""
    try:
        app = request.app
    except (KeyError, AttributeError):
        return None, None
    routes = getattr(app, "routes", [])
    for route in routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            path = getattr(route, "path", None)
            name = getattr(route, "name", None)
            return path, name
    return None, None


class StarletteHoneybadger(BaseHTTPMiddleware):
    """Starlette middleware for Honeybadger error and event tracking."""

    def __init__(self, app: ASGIApp, **kwargs):
        if kwargs:
            honeybadger.configure(**kwargs)

        if "Starlette" not in default_plugin_manager._registered:
            default_plugin_manager.register(StarlettePlugin())

        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.monotonic()
        request_id = sanitize_request_id(request.headers.get("x-request-id"))
        if not request_id:
            request_id = str(uuid.uuid4())

        honeybadger.begin_request(request)
        honeybadger.set_event_context(request_id=request_id)

        token = _current_request.set(request)

        status_code = 500
        try:
            # BaseHTTPMiddleware buffers the response body, so call_next
            # returns after the full response is generated. Duration
            # measured in the finally block reflects request processing
            # time but excludes any BackgroundTask execution.
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            # Skip HTTP exceptions (4xx errors etc.) like FastAPI integration
            try:
                from starlette.exceptions import HTTPException

                if isinstance(exc, HTTPException):
                    status_code = exc.status_code
                    raise
            except ImportError:
                pass

            scope = dict(request.scope)
            try:
                body = await request.body()
                scope["body"] = body
            except Exception:
                pass
            honeybadger.notify(exception=exc, context=_as_context(scope))
            raise
        finally:
            _current_request.reset(token)
            try:
                starlette_config = honeybadger.config.insights_config.starlette
                if (
                    honeybadger.config.insights_enabled
                    and not starlette_config.disabled
                ):
                    route_path, route_name = _match_route(request)
                    payload = {
                        "method": request.method,
                        "path": request.url.path,
                        "status": status_code,
                        "duration": get_duration(start),
                    }
                    if route_path:
                        payload["route"] = route_path
                    if route_name:
                        payload["view"] = route_name

                    if starlette_config.include_params:
                        params = {}
                        for key in request.query_params:
                            values = request.query_params.getlist(key)
                            params[key] = values[0] if len(values) == 1 else values
                        payload["params"] = filter_dict(
                            params,
                            honeybadger.config.params_filters,
                            remove_keys=True,
                        )

                    honeybadger.event("starlette.request", payload)
                honeybadger.reset_context()
                honeybadger.reset_event_context()
            except Exception as e:
                logger.warning(
                    f"Exception while sending Honeybadger event: {e}", exc_info=True
                )
