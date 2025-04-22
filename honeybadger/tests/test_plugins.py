import unittest

from mock import Mock
from honeybadger.plugins import PluginManager


class PluginManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.manager = PluginManager()
        self.plugin1 = Mock()
        self.plugin1.name = "plugin1"
        self.plugin2 = Mock()
        self.plugin2.name = "plugin2"
        self.default_payload = {}

    def test_register(self):
        self.manager.register(self.plugin1)
        self.manager.register(self.plugin2)
        self.manager.register(self.plugin1)

        self.assertListEqual(
            list(self.manager._registered.keys()), ["plugin1", "plugin2"]
        )
        self.assertListEqual(
            list(self.manager._registered.values()), [self.plugin1, self.plugin2]
        )

    def test_generate_payload_first_plugin(self):
        self.manager.register(self.plugin1)
        self.manager.register(self.plugin2)
        context = {"test": "context"}

        # Given both plugin support providing payload
        self.plugin1.supports.return_value = True
        self.plugin1.generate_payload.return_value = {"name": "plugin1"}
        self.plugin2.supports.return_value = True
        self.plugin2.generate_payload.return_value = {"name": "plugin2"}

        payload = self.manager.generate_payload(self.default_payload, context=context)
        # Expect order to be preferred and use value from all registered plugins.
        # Plugin2 is given a forced return value for the sake of testing,
        # hence it overrides payload from plugin1
        self.assertDictEqual({"name": "plugin2"}, payload)
        self.plugin1.supports.assert_called_once_with(None, context)
        self.plugin1.generate_payload.assert_called_once_with(
            self.default_payload, None, context
        )

        # Assert both plugins got called once
        self.assertEqual(1, self.plugin2.supports.call_count)
        self.assertEqual(1, self.plugin2.generate_payload.call_count)

    def test_generate_payload_second_plugin(self):
        self.manager.register(self.plugin1)
        self.manager.register(self.plugin2)
        context = {"test": "context"}

        # Given only 2nd registered plugin supports providing payload
        self.plugin1.supports.return_value = False
        self.plugin2.supports.return_value = True
        self.plugin2.generate_payload.return_value = {"name": "plugin2"}

        payload = self.manager.generate_payload(
            default_payload=self.default_payload, context=context
        )
        # Expect order to be preferred and use value from second plugin
        self.assertDictEqual({"name": "plugin2"}, payload)
        self.plugin1.supports.assert_called_once_with(None, context)
        self.assertEqual(0, self.plugin1.generate_payload.call_count)
        self.plugin2.supports.assert_called_once_with(None, context)
        self.plugin2.generate_payload.assert_called_once_with(
            self.default_payload, None, context
        )

    def test_generate_payload_none(self):
        self.manager.register(self.plugin1)
        self.manager.register(self.plugin2)
        context = {"test": "context"}
        default_payload = {"request": {"context": context}}

        # Given no registered plugin supports providing payload
        self.plugin1.supports.return_value = False
        self.plugin2.supports.return_value = False

        payload = self.manager.generate_payload(
            default_payload=default_payload, context=context
        )
        print(payload)
        # Expect order to be preferred and use input context value
        self.assertDictEqual({"request": {"context": context}}, payload)
        self.plugin1.supports.assert_called_once_with(None, context)
        self.assertEqual(0, self.plugin1.generate_payload.call_count)
        self.plugin2.supports.assert_called_once_with(None, context)
        self.assertEqual(0, self.plugin2.generate_payload.call_count)
