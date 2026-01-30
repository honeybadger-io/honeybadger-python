import threading
from contextlib import contextmanager
import sys
import logging
import datetime
import atexit
import uuid
import hashlib

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
            return None

        if notice.excluded_exception():
            logger.debug("Notice was excluded by exception filter")
            return None

        return self._connection().send_notice(self.config, notice)

    def begin_request(self, _):
        error_context.clear()
        event_context.clear()

    def wrap_excepthook(self, func):
        self.existing_except_hook = func
        sys.excepthook = self.exception_hook

    def exception_hook(self, type, exception, exc_traceback):
        self.notify(exception=exception)
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

        request_id = self._get_event_context().get("request_id", None)

        notice = Notice(
            exception=exception,
            error_class=error_class,
            error_message=error_message,
            context=merged_ctx,
            fingerprint=fingerprint,
            tags=merged_tags,
            config=self.config,
            request_id=request_id,
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

        if callable(self.config.before_event):
            try:
                next_payload = self.config.before_event(payload)
                if next_payload is False:
                    return  # Skip sending the event
                elif next_payload is not payload and next_payload is not None:
                    payload = next_payload  # Overwrite payload
                # else: assume in-place mutation; keep payload as-is
            except Exception as e:
                logger.error("Error in before_event callback: %s", e)

        # Add a timestamp to the payload if not provided
        if "ts" not in payload:
            payload["ts"] = datetime.datetime.now(datetime.timezone.utc)
        if isinstance(payload["ts"], datetime.datetime):
            payload["ts"] = payload["ts"].strftime(self.TS_FORMAT)

        final_payload = {**self._get_event_context(), **payload}

        # Check sampling on the final merged payload
        if not self._should_sample_event(final_payload):
            return

        # Strip internal _hb metadata before sending
        final_payload.pop("_hb", None)

        return self.events_worker.push(final_payload)

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

    def _should_sample_event(self, payload):
        """
        Determine if an event should be sampled based on sample rate and payload metadata.
        Returns True if the event should be sent, False if it should be skipped.
        """
        # Get sample rate from payload _hb override or global config
        hb_metadata = payload.get("_hb", {})
        sample_rate = hb_metadata.get("sample_rate", self.config.events_sample_rate)

        if sample_rate >= 100:
            return True

        if sample_rate <= 0:
            return False

        sampling_key = payload.get("request_id")
        if not sampling_key:
            sampling_key = str(uuid.uuid4())
        hash_value = int(hashlib.md5(sampling_key.encode()).hexdigest(), 16)
        return (hash_value % 100) < sample_rate

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

    def set_event_context(self, ctx: Optional[Dict[str, Any]] = None, **kwargs):
        event_context.update(ctx, **kwargs)

    def reset_event_context(self):
        event_context.clear()

    @contextmanager
    def event_context(self, ctx: Optional[Dict[str, Any]] = None, **kwargs):
        with event_context.override(ctx, **kwargs):
            yield

    def _connection(self):
        if self.config.is_dev() and not self.config.force_report_data:
            return fake_connection
        else:
            return connection
