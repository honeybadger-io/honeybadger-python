from abc import ABCMeta, abstractmethod
from collections import OrderedDict
from logging import getLogger

from six import add_metaclass, iteritems

logger = getLogger(__name__)


@add_metaclass(ABCMeta)
class Plugin(object):
    """
    Base class for plugins. A plugin is used to add functionality related to frameworks.
    """

    def __init__(self, name):
        """
        Initialize plugin.
        :param name: the name of the plugin.
        """
        self.name = name

    def supports(self, config, context):
        """
        Whether this plugin supports generating payload for the current configuration, request and context.
        :param exception: current exception.
        :param config: honeybadger configuration.
        :param context: current honeybadger context.
        :return: True if plugin can generate payload for current exception, False else.
        """
        return False

    @abstractmethod
    def generate_payload(self, config, context):
        """
        Return additional payload for given exception. May be used by actual plugin implementations to gather additional
        information.
        :param config: honeybadger configuration
        :param context: context gathered so far to send to honeybadger.
        :return: a dictionary with the generated payload.
        """
        pass


class PluginManager(object):
    """
    Manages lifecycle of plugins.
    """

    def __init__(self):
        self._registered = OrderedDict()

    def register(self, plugin):
        """
        Register the given plugin. Registration order is kept.
        :param plugin: the plugin to register.
        """
        if plugin.name not in self._registered:
            logger.info('Registering plugin %s' % plugin.name)
            self._registered[plugin.name] = plugin
        else:
            logger.warning('Plugin %s already registered' % plugin.name)

    def generate_payload(self, default_payload, config=None, context=None):
        """
        Generate payload by iterating over registered plugins. Merges .
        :param context: current context.
        :param config: honeybadger configuration.
        :return: a dict with the generated payload.
        """
        for name, plugin in iteritems(self._registered):
            if plugin.supports(config, context):
                logger.debug('Returning payload from plugin %s' % name)

                default_payload = plugin.generate_payload(default_payload, config, context)
        else:
            logger.debug('No active plugin to generate payload')

        return default_payload


# Global plugin manager
default_plugin_manager = PluginManager()
