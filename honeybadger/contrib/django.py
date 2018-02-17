import re

from honeybadger.plugins import Plugin
from honeybadger.utils import filter_dict


class DjangoPlugin(Plugin):
    """
    Plugin for generating payload from Django requests.
    """
    def __init__(self):
        super(DjangoPlugin, self).__init__('Django')

    def supports(self, config, request, context):
        """
        Check whether this is a django request or not.
        :param config: honeybadger configuration.
        :param request: the request to handle.
        :param context: current honeybadger configuration.
        :return: True if this is a django request, False else.
        """
        return request is not None and re.match(r'^django\.', request.__module__)

    def generate_payload(self, config, request, context):
        """
        Generate payload by checking Django request object.
        :param context: current context.
        :param request: the request object.
        :param config: honeybadger configuration.
        :return: a dict with the generated payload.
        """

        payload = {
            'url': request.build_absolute_uri(),
            'component': request.resolver_match.app_name,
            'action': request.resolver_match.func.__name__,
            'params': {},
            'session': {},
            'cgi_data': dict(request.META),
            'context': context
        }

        if hasattr(request, 'session'):
            payload['session'] = filter_dict(dict(request.session), config.params_filters)

        if request.method == 'POST':
            payload['params'] = filter_dict(dict(request.POST), config.params_filters)
        else:
            payload['params'] = filter_dict(dict(request.GET), config.params_filters)

        return payload
