import threading
from contextlib import contextmanager
import sys
import logging
import datetime
import atexit
from typing import Optional, Dict, Any, List

from honeybadger.plugins import default_plugin_manager
import honeybadger.connection as connection
import honeybadger.fake_connection as fake_connection
from .events_worker import EventsWorker
from .config import Configuration
from .notice import Notice
from .context_store import ContextStore

logger = logging.getLogger("honeybadger")
logger.addHandler(logging.NullHandler())

error_context = ContextStore("honeybadger_error_context")
event_context = ContextStore("honeybadger_event_context")


class Honeybadger(object):
    TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

    def __init__(self):
        error_context.clear()
        event_context.clear()

        self.config = Configuration()
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

    def begin_request(self, _):
        error_context.clear()
        event_context.clear()

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
        context: Optional[Dict[str, Any]] = None,
        fingerprint=None,
        tags: Optional[List[str]] = None,
    ):
        base = error_context.get()
        tag_ctx = base.pop("_tags", [])
        merged_ctx = {**base, **(context or {})}
        merged_tags = list({*tag_ctx, *(tags or [])})

        notice = Notice(
            exception=exception,
            error_class=error_class,
            error_message=error_message,
            context=merged_ctx,
            fingerprint=fingerprint,
            tags=merged_tags,
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

    # Error context
    #
    def _get_context(self):
        return error_context.get()

    def set_context(self, ctx: Optional[Dict[str, Any]] = None, **kwargs):
        error_context.update(ctx, **kwargs)

    def reset_context(self):
        error_context.clear()

    @contextmanager
    def context(self, ctx: Optional[Dict[str, Any]] = None, **kwargs):
        with error_context.override(ctx, **kwargs):
            yield

    # Event context
    #
    def _get_event_context(self):
        return event_context.get()

    def set_event_context(self, **kwargs):
        event_context.update(**kwargs)

    def _connection(self):
        if self.config.is_dev() and not self.config.force_report_data:
            return fake_connection
        else:
            return connection
