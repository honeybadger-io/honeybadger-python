from __future__ import absolute_import
import re
import time
import uuid

from six import iteritems

from honeybadger import honeybadger
from honeybadger.plugins import Plugin, default_plugin_manager
from honeybadger.utils import (
    filter_dict,
    filter_env_vars,
    get_duration,
    sanitize_request_id,
)
from honeybadger.contrib.db import DBHoneybadger

try:
    from threading import local  # type: ignore[no-redef]
except ImportError:
    from django.utils._threading_local import local  # type: ignore[no-redef,import]

_thread_locals = local()

REQUEST_LOCAL_KEY = "__django_current_request"


def current_request():
    """
    Return current request for this thread.
    :return: current request for this thread.
    """
    return getattr(_thread_locals, REQUEST_LOCAL_KEY, None)


def set_request(request):
    """
    Set request for current thread.
    :param request: current request.
    """
    setattr(_thread_locals, REQUEST_LOCAL_KEY, request)


def clear_request():
    """
    Clears request for this thread.
    """
    if hasattr(_thread_locals, REQUEST_LOCAL_KEY):
        setattr(_thread_locals, REQUEST_LOCAL_KEY, None)


class DjangoPlugin(Plugin):
    """
    Plugin for generating payload from Django requests.
    """

    def __init__(self):
        super(DjangoPlugin, self).__init__("Django")

    def supports(self, config, context):
        """
        Check whether this is a django request or not.
        :param config: honeybadger configuration.
        :param context: current honeybadger configuration.
        :return: True if this is a django request, False else.
        """
        request = current_request()
        return request is not None and re.match(r"^django\.", request.__module__)

    def generate_payload(self, default_payload, config, context):
        """
        Generate payload by checking Django request object.
        :param context: current context.
        :param config: honeybadger configuration.
        :return: a dict with the generated payload.
        """
        import django

        if django.VERSION[0] < 2:
            # pylint: disable-next=import-error,no-name-in-module
            from django.core.urlresolvers import resolve  # type: ignore[import]
        else:
            from django.urls import resolve

        request = current_request()
        resolver_match = request.resolver_match or resolve(request.path_info)
        request_payload = {
            "url": request.build_absolute_uri(),
            "component": resolver_match.app_name,
            "action": resolver_match.func.__name__,
            "params": {},
            "session": {},
            "cgi_data": filter_dict(
                filter_env_vars(request.META), config.params_filters
            ),
            "context": context,
        }

        if hasattr(request, "session"):
            request_payload["session"] = filter_dict(
                dict(request.session), config.params_filters
            )

        if hasattr(request, "COOKIES"):
            request_payload["cgi_data"]["HTTP_COOKIE"] = filter_dict(
                request.COOKIES, config.params_filters
            )

        if request.method == "GET":
            request_payload["params"] = filter_dict(
                dict(request.GET), config.params_filters
            )

        else:
            request_payload["params"] = filter_dict(
                dict(request.POST), config.params_filters
            )

        default_payload["request"].update(request_payload)

        return default_payload


class DjangoHoneybadgerMiddleware(object):
    def __init__(self, get_response=None):
        self.get_response = get_response
        from django.conf import settings

        if getattr(settings, "DEBUG"):
            honeybadger.configure(environment="development")
        config_kwargs = dict(
            [
                (k.lower(), v)
                for (k, v) in iteritems(getattr(settings, "HONEYBADGER", {}))
            ]
        )
        honeybadger.configure(**config_kwargs)
        honeybadger.config.set_12factor_config()  # environment should override Django settings
        default_plugin_manager.register(DjangoPlugin())
        if honeybadger.config.insights_enabled:
            self._patch_cursor()

    def __call__(self, request):
        set_request(request)
        start_time = time.time()
        honeybadger.begin_request(request)
        self._set_request_id(request)
        response = self.get_response(request)

        if (
            honeybadger.config.insights_enabled
            and not honeybadger.config.insights_config.django.disabled
        ):
            self._send_request_event(request, response, start_time)

        honeybadger.reset_context()
        clear_request()

        return response

    def _set_request_id(self, request):
        # Attempt to get request ID from various sources
        request_id = (
            getattr(request, "id", None)
            or getattr(request, "request_id", None)
            or request.headers.get("X-Request-ID", None)
        )
        request_id = sanitize_request_id(request_id)
        if not request_id:
            request_id = str(uuid.uuid4())

        honeybadger.set_event_context(request_id=request_id)

    def _patch_cursor(self):
        from django.db.backends.utils import CursorWrapper

        orig_exec = CursorWrapper.execute
        CursorWrapper.execute = DBHoneybadger.django_execute(orig_exec)

    def _send_request_event(self, request, response, start_time):
        # Get resolver data
        resolver_match = getattr(request, "resolver_match", None)
        view_name = None
        view_module = None
        app_name = None

        if resolver_match:
            view_name = getattr(resolver_match, "view_name", None)
            if not view_name and hasattr(resolver_match, "func"):
                view_name = resolver_match.func.__name__

            if hasattr(resolver_match, "func"):
                view_module = resolver_match.func.__module__

            app_name = getattr(resolver_match, "app_name", None)

        request_data = {
            "path": request.path_info if hasattr(request, "path_info") else None,
            "method": request.method if hasattr(request, "method") else None,
            "status": (
                response.status_code if hasattr(response, "status_code") else None
            ),
            "view": view_name,
            "module": view_module,
            "app": app_name,
            "duration": get_duration(start_time),
        }

        if honeybadger.config.insights_config.django.include_params:
            params = {}
            for qd in [request.GET, request.POST]:
                for key in qd:
                    values = qd.getlist(key)
                    params[key] = values[0] if len(values) == 1 else values
            filtered = filter_dict(
                params, honeybadger.config.params_filters, remove_keys=True
            )
            request_data["params"] = filtered

        honeybadger.event("django.request", request_data)

    def process_exception(self, request, exception):
        self.__set_user_from_context(request)
        honeybadger.notify(exception)
        clear_request()
        return None

    def __set_user_from_context(self, request):
        # in Django 1 request.user.is_authenticated is a function, in Django 2+ it's a boolean
        if hasattr(request, "user") and (
            (
                isinstance(request.user.is_authenticated, bool)
                and request.user.is_authenticated
            )
            or (
                callable(request.user.is_authenticated)
                and request.user.is_authenticated()
            )
        ):
            honeybadger.set_context(username=request.user.get_username())
            honeybadger.set_context(user_id=request.user.id)
