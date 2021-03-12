import threading
from contextlib import contextmanager
import sys
import logging
import copy

from honeybadger.plugins import default_plugin_manager
import honeybadger.connection as connection
import honeybadger.fake_connection as fake_connection
from .payload import create_payload
from .config import Configuration

logging.getLogger('honeybadger').addHandler(logging.NullHandler())


class Honeybadger(object):
    def __init__(self):
        self.config = Configuration()
        self.thread_local = threading.local()
        self.thread_local.context = {}

    def _send_notice(self, exception, exc_traceback=None, context={}):
        payload = create_payload(exception, exc_traceback, config=self.config, context=context)
        if self.config.is_dev() and not self.config.force_report_data:
            fake_connection.send_notice(self.config, payload)
        else:
            connection.send_notice(self.config, payload)

    def _get_context(self):
        return getattr(self.thread_local, 'context', {})

    def begin_request(self, request):
        self.thread_local.context = self._get_context()

    def wrap_excepthook(self, func):
        self.existing_except_hook = func
        sys.excepthook = self.exception_hook

    def exception_hook(self, type, value, exc_traceback):
        self._send_notice(value, exc_traceback, context=self._get_context())
        self.existing_except_hook(type, value, exc_traceback)

    def notify(self, exception=None, error_class=None, error_message=None, context={}):
        if exception is None:
            exception = {
                'error_class': error_class,
                'error_message': error_message
            }

        merged_context = self._get_context()
        merged_context.update(context)

        self._send_notice(exception, context=merged_context)

    def configure(self, **kwargs):
        self.config.set_config_from_dict(kwargs)
        self.auto_discover_plugins()

    def auto_discover_plugins(self):
        #Avoiding circular import error
        from honeybadger import contrib
        
        if self.config.is_aws_lambda_environment:
            default_plugin_manager.register(contrib.AWSLambdaPlugin())

    def set_context(self, **kwargs):
        # This operation is an update, not a set!
        self.thread_local.context = self._get_context()
        self.thread_local.context.update(kwargs)

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
