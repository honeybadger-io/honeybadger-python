from .contrib.django import DjangoHoneybadgerMiddleware as RealDjangoHoneybadgerMiddleware
import warnings


class DjangoHoneybadgerMiddleware(RealDjangoHoneybadgerMiddleware):
    def __init__(self, *args, **kwargs):
        warnings.warn("DjangoHoneybadgerMiddleware has moved! Update your imports to import it from honeybadger.contrib", FutureWarning)
        super(DjangoHoneybadgerMiddleware, self).__init__(*args, **kwargs)
