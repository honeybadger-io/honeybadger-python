import logging
from honeybadger.core import Honeybadger

DEFAULT_IGNORED_KEYS = {
    'process',
    'thread',
    'levelno',
    'pathname',
    'module',
    'filename',
    'funcName',
    'asctime',
    'msecs',
    'processName',
    'relativeCreated',
    'threadName',
    'stack_info',
    'exc_info',
    'exc_text',
    'args',
    'msg',
    'message',
}


class HoneybadgerHandler(logging.Handler):

    def __init__(self, api_key):

        self.honeybadger = Honeybadger()
        self.honeybadger.configure(api_key=api_key)

        logging.Handler.__init__(self)

    def _get_context(self, record):
        return {
            k: v for (k, v) in record.__dict__.items()
            if k not in DEFAULT_IGNORED_KEYS
        }

    def emit(self, record):

        try:
            self.honeybadger.notify(
                error_class="%s Log" % record.levelname,
                error_message=record.getMessage(),
                context=self._get_context(record)
            )

        except Exception:
            self.handleError(record)
