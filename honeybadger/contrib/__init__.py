from honeybadger.contrib.flask import FlaskHoneybadger
from honeybadger.contrib.django import DjangoHoneybadgerMiddleware
from honeybadger.contrib.aws_lambda import AWSLambdaPlugin
from honeybadger.contrib.logger import HoneybadgerHandler
from honeybadger.contrib.asgi import ASGIHoneybadger
from honeybadger.contrib.celery import CeleryHoneybadger

_has_starlette = False
try:
    from honeybadger.contrib.starlette import StarletteHoneybadger

    _has_starlette = True
except ImportError:
    pass

__all__ = [
    "FlaskHoneybadger",
    "DjangoHoneybadgerMiddleware",
    "AWSLambdaPlugin",
    "HoneybadgerHandler",
    "ASGIHoneybadger",
    "CeleryHoneybadger",
]

if _has_starlette:
    __all__.append("StarletteHoneybadger")
