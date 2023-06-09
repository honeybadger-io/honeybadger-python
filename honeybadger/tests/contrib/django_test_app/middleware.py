from django.utils.deprecation import MiddlewareMixin
from honeybadger import honeybadger


class CustomMiddleware(MiddlewareMixin):
    def process_request(self, request):
        honeybadger.notify("Custom Middleware Exception")
