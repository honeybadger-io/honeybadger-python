import logging

from fastapi.routing import APIRoute
from fastapi import exceptions
from starlette.requests import Request
from typing import Callable

from honeybadger import honeybadger
from honeybadger.contrib import asgi


class HoneybadgerRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request):
            try:
                return await original_route_handler(request)
            except exceptions.HTTPException as exc:
                raise exc from None
            except Exception as exc:
                body = await request.body()
                scope = dict(request)
                scope["body"] = body
                honeybadger.notify(exception=exc, context=asgi._as_context(scope))
                raise exc from None
            finally:
                honeybadger.reset_context()
        return custom_route_handler
