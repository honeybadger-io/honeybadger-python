"""
Use cases:

>>> from honeybadger import honeybadger
>>> honeybadger.notify()
>>> honeybadger.configure(**kwargs)
>>> honeybadger.context(**kwargs)
"""

import sys
import signal
import threading
from .core import Honeybadger
from .version import __version__

__all__ = ["honeybadger", "__version__"]

honeybadger = Honeybadger()
honeybadger.wrap_excepthook(sys.excepthook)


def _register_signal_handler():
    orig = signal.getsignal(signal.SIGTERM)

    def _on_term(signum, frame):
        if callable(orig):
            orig(signum, frame)
        else:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)


if threading.current_thread() is threading.main_thread():
    _register_signal_handler()
