"""
Use cases:

>>> from honeybadger import honeybadger
>>> honeybadger.notify()
>>> honeybadger.configure(**kwargs)
>>> honeybadger.context(**kwargs)
"""

import sys
from .core import Honeybadger
from .version import __version__

__all__ = ["honeybadger", "__version__"]

honeybadger = Honeybadger()
honeybadger.wrap_excepthook(sys.excepthook)
