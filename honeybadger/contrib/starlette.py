import logging
import time
import uuid
from contextvars import ContextVar
from typing import Optional

from honeybadger import honeybadger
from honeybadger.plugins import Plugin, default_plugin_manager
from honeybadger.utils import filter_dict, get_duration, sanitize_request_id
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

_plugin_registered = False


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

        cgi_data = {k: v for k, v in request.headers.items()}
        cgi_data["REQUEST_METHOD"] = request.method

        params = {}
        for key in request.query_params:
            values = request.query_params.getlist(key)
            params[key] = values if len(values) > 1 else values[0] if values else None

        payload = {
            "url": str(request.url),
            "component": route or request.url.path,
            "action": route_name or request.method,
            "params": filter_dict(params, config.params_filters),
            "cgi_data": filter_dict(cgi_data, config.params_filters),
            "context": context,
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

        global _plugin_registered
        if not _plugin_registered:
            default_plugin_manager.register(StarlettePlugin())
            _plugin_registered = True

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
            except Exception as e:
                logger.warning(
                    f"Exception while sending Honeybadger event: {e}", exc_info=True
                )
