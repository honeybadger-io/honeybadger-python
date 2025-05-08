import threading
from contextlib import contextmanager
import sys
import logging
import copy
import time
import datetime
import atexit

from honeybadger.plugins import default_plugin_manager
import honeybadger.connection as connection
import honeybadger.fake_connection as fake_connection
from .events_worker import EventsWorker
from .config import Configuration
from .notice import Notice

logger = logging.getLogger("honeybadger")
logger.addHandler(logging.NullHandler())


class Honeybadger(object):
    TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

    def __init__(self):
        self.config = Configuration()
        self.thread_local = threading.local()
        self.thread_local.context = {}
        self.events_worker = EventsWorker(
            self._connection(), self.config, logger=logging.getLogger("honeybadger")
        )
        atexit.register(self.shutdown)

    def _send_notice(self, notice):
        if callable(self.config.before_notify):
            try:
                notice = self.config.before_notify(notice)
            except Exception as e:
                logger.error("Error in before_notify callback: %s", e)

        if not isinstance(notice, Notice):
            logger.debug("Notice was filtered out by before_notify callback")
            return

        if notice.excluded_exception():
            logger.debug("Notice was excluded by exception filter")
            return

        self._connection().send_notice(self.config, notice)

    def _get_context(self):
        return getattr(self.thread_local, "context", {})

    def begin_request(self, request):
        self.thread_local.context = self._get_context()

    def wrap_excepthook(self, func):
        self.existing_except_hook = func
        sys.excepthook = self.exception_hook

    def exception_hook(self, type, exception, exc_traceback):
        notice = Notice(
            exception=exception, thread_local=self.thread_local, config=self.config
        )
        self._send_notice(notice)
        self.existing_except_hook(type, exception, exc_traceback)

    def shutdown(self):
        self.events_worker.shutdown()

    def notify(
        self,
        exception=None,
        error_class=None,
        error_message=None,
        context={},
        fingerprint=None,
        tags=[],
    ):
        notice = Notice(
            exception=exception,
            error_class=error_class,
            error_message=error_message,
            context=context,
            fingerprint=fingerprint,
            tags=tags,
            thread_local=self.thread_local,
            config=self.config,
        )
        return self._send_notice(notice)

    def event(self, event_type=None, data=None, **kwargs):
        """
        Send an event to Honeybadger.
        Events logged with this method will appear in Honeybadger Insights.
        """
        # If the first argument is a string, treat it as event_type
        if isinstance(event_type, str):
            payload = data.copy() if data else {}
            payload["event_type"] = event_type
        # If the first argument is a dictionary, merge it with kwargs
        elif isinstance(event_type, dict):
            payload = event_type.copy()
            payload.update(kwargs)
        # Raise an error if event_type is not provided correctly
        else:
            raise ValueError(
                "The first argument must be either a string or a dictionary"
            )

        # Add a timestamp to the payload if not provided
        if "ts" not in payload:
            payload["ts"] = datetime.datetime.now(datetime.timezone.utc)
        if isinstance(payload["ts"], datetime.datetime):
            payload["ts"] = payload["ts"].strftime(self.TS_FORMAT)

        return self.events_worker.push(payload)

    def configure(self, **kwargs):
        self.config.set_config_from_dict(kwargs)
        self.auto_discover_plugins()

        # Update events worker with new config
        self.events_worker.connection = self._connection()
        self.events_worker.config = self.config

    def auto_discover_plugins(self):
        # Avoiding circular import error
        from honeybadger import contrib

        if self.config.is_aws_lambda_environment:
            default_plugin_manager.register(contrib.AWSLambdaPlugin())

    def set_context(self, ctx=None, **kwargs):
        # This operation is an update, not a set!
        if not ctx:
            ctx = kwargs
        else:
            ctx.update(kwargs)
        self.thread_local.context = self._get_context()
        self.thread_local.context.update(ctx)

    def reset_context(self):
        self.thread_local.context = {}

    @contextmanager
    def context(self, **kwargs):
        original_context = copy.copy(self._get_context())
        self.set_context(**kwargs)
        try:
            yield
        except:
            raise
        else:
            self.thread_local.context = original_context

    def _connection(self):
        if self.config.is_dev() and not self.config.force_report_data:
            return fake_connection
        else:
            return connection
