from __future__ import absolute_import

import logging

from honeybadger import honeybadger
from honeybadger.plugins import Plugin, default_plugin_manager
from honeybadger.utils import filter_dict
from six import iteritems

logger = logging.getLogger(__name__)


class FlaskPlugin(Plugin):
    """
    Handle flask plugin information.
    """

    def __init__(self):
        super(FlaskPlugin, self).__init__('Flask')

    def supports(self, config, context):
        """
        Check whether we are in a Flask request context.
        :param config: honeybadger configuration.
        :param context: current honeybadger configuration.
        :return: True if this is a django request, False else.
        """
        try:
            from flask import request
        except ImportError:
            return False
        else:
            return bool(request)

    def generate_payload(self, default_payload, config, context):
        """
        Generate payload by checking Flask request object.
        :param context: current context.
        :param config: honeybadger configuration.
        :return: a dict with the generated payload.
        """
        from flask import current_app, session, request as _request

        current_view = current_app.view_functions[_request.endpoint]
        if hasattr(current_view, 'view_class'):
            component = '.'.join((current_view.__module__, current_view.view_class.__name__))
        else:
            component = current_view.__module__
        cgi_data = {
            k: v
            for k, v in iteritems(_request.headers)
        }
        cgi_data.update({
            'REQUEST_METHOD': _request.method,
            'HTTP_COOKIE': dict(_request.cookies)
        })
        payload = {
            'url': _request.base_url,
            'component': component,
            'action': _request.endpoint,
            'params': {},
            'session': filter_dict(dict(session), config.params_filters),
            'cgi_data': filter_dict(cgi_data, config.params_filters),
            'context': context
        }

        # Add query params
        params = filter_dict(_request.args.to_dict(flat=False), config.params_filters)
        params.update(filter_dict(_request.form.to_dict(flat=False), config.params_filters))

        payload['params'] = params

        default_payload['request'].update(payload)

        return default_payload


class FlaskHoneybadger(object):
    """
    Flask extension for Honeybadger. Initializes Honeybadger and adds a request information to payload.
    """
    CONFIG_PREFIX = 'HONEYBADGER_'

    def __init__(self, app=None, report_exceptions=False, reset_context_after_request=False):
        """
        Initialize Honeybadger.
        :param flask.Application app: the application to wrap for the exception.
        :param bool report_exceptions: whether to automatically report exceptions raised by Flask on requests
         (i.e. by calling abort) or not.
        :param bool reset_context_after_request: whether to reset honeybadger context after each request.
        """
        self.app = app
        self.report_exceptions = False
        self.reset_context_after_request = False
        default_plugin_manager.register(FlaskPlugin())
        if app is not None:
            self.init_app(app,
                          report_exceptions=report_exceptions,
                          reset_context_after_request=reset_context_after_request)

    def init_app(self, app, report_exceptions=False, reset_context_after_request=False):
        """
        Initialize honeybadger and listen for errors.
        :param Flask app: the Flask application object.
        :param bool report_exceptions: whether to automatically report exceptions raised by Flask on  requests
         (i.e. by calling abort) or not.
        :param bool reset_context_after_request: whether to reset honeybadger context after each request.
        """
        from flask import request_tearing_down, got_request_exception

        self.app = app

        self.app.logger.info('Initializing Honeybadger')

        self.report_exceptions = report_exceptions
        self.reset_context_after_request = reset_context_after_request
        self._initialize_honeybadger(app.config)

        # Add hooks
        if self.report_exceptions:
            self._register_signal_handler('auto-reporting exceptions',
                                          got_request_exception,
                                          self._handle_exception)

        if self.reset_context_after_request:
            self._register_signal_handler('auto clear context on request end',
                                          request_tearing_down,
                                          self._reset_context)

        logger.info('Honeybadger helper installed')

    def _register_signal_handler(self, description, signal, handler):
        """
        Registers a handler for the given signal.
        :param description: a short description of the signal to handle.
        :param signal: the signal to handle.
        :param handler: the function to use for handling the signal.
        """
        from flask import signals

        if hasattr(signals, 'signals_available') and not signals.signals_available:
            self.app.logger.warn('blinker needs to be installed in order to support %s'.format(description))
        self.app.logger.info('Enabling {}'.format(description))
        # Weak references won't work if handlers are methods rather than functions.
        signal.connect(handler, sender=self.app, weak=False)

    def _initialize_honeybadger(self, config):
        """
        Initializes honeybadger using the given config object.
        :param dict config: a dict or dict-like object that contains honeybadger configuration properties.
        """
        if config.get('DEBUG', False):
            honeybadger.configure(environment='development')

        honeybadger_config = {}
        for key, value in iteritems(config):
            if key.startswith(self.CONFIG_PREFIX):
                honeybadger_config[key[len(self.CONFIG_PREFIX):].lower()] = value

        honeybadger.configure(**honeybadger_config)
        honeybadger.config.set_12factor_config()  # environment should override Flask settings

    def _reset_context(self, *args, **kwargs):
        """
        Resets context when request is done.
        """
        honeybadger.reset_context()

    def _handle_exception(self, sender, exception=None):
        """
        Actual code handling the exception and sending it to honeybadger if it's enabled.
        :param T sender: the object sending the exception event.
        :param Exception exception: the exception to handle.
        """
        honeybadger.notify(exception)
        if self.reset_context_after_request:
            self._reset_context()
