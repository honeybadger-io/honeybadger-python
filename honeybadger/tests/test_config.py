from __future__ import print_function

import os
import pytest

from honeybadger.config import Configuration


def test_12factor_overrides_defaults():
    os.environ["HONEYBADGER_ENVIRONMENT"] = "staging"
    c = Configuration()
    assert c.environment == "staging"


def test_args_overrides_defaults():
    c = Configuration(environment="staging")
    assert c.environment == "staging"


def test_args_overrides_12factor():
    os.environ["HONEYBADGER_ENVIRONMENT"] = "test"
    c = Configuration(environment="staging")
    assert c.environment == "staging"


def test_config_var_types_are_accurate():
    os.environ["HONEYBADGER_PARAMS_FILTERS"] = "password,password_confirm,user_email"
    c = Configuration()
    assert c.params_filters == ["password", "password_confirm", "user_email"]


def test_config_bool_types_are_accurate():
    os.environ["HONEYBADGER_FORCE_REPORT_DATA"] = "1"
    c = Configuration()
    del os.environ["HONEYBADGER_FORCE_REPORT_DATA"]
    assert c.force_report_data == True


def test_can_only_set_valid_options():
    c = Configuration(foo="bar")
    with pytest.raises(AttributeError):
        # pylint: disable-next=no-member
        print(c.foo)


def test_valid_dev_environments():
    valid_dev_environments = ["development", "dev", "test"]

    assert len(Configuration.DEVELOPMENT_ENVIRONMENTS) == len(valid_dev_environments)
    assert set(Configuration.DEVELOPMENT_ENVIRONMENTS) == set(valid_dev_environments)


def test_override_development_environments():
    custom_dev_envs = ["local", "staging"]
    c = Configuration(development_environments=custom_dev_envs)
    assert c.development_environments == custom_dev_envs


def test_is_dev_true_for_dev_environments():
    for env in Configuration.DEVELOPMENT_ENVIRONMENTS:
        c = Configuration(environment=env)
        assert c.is_dev()


def test_is_dev_false_for_non_dev_environments():
    c = Configuration(environment="production")
    assert c.is_dev() == False


def test_is_dev_true_for_custom_dev_environments():
    custom_dev_envs = ["local", "staging"]
    c = Configuration(environment="local", development_environments=custom_dev_envs)
    assert c.is_dev() == True

    c = Configuration(environment="staging", development_environments=custom_dev_envs)
    assert c.is_dev() == True


def test_is_dev_false_for_custom_non_dev_environments():
    custom_dev_envs = ["local", "staging"]
    c = Configuration(
        environment="production", development_environments=custom_dev_envs
    )
    assert c.is_dev() == False

    c = Configuration(environment="qa", development_environments=custom_dev_envs)
    assert c.is_dev() == False


def test_force_report_data_not_active():
    c = Configuration()
    assert c.force_report_data == False


def test_configure_before_notify():
    def before_notify_callback(notice):
        return notice

    c = Configuration(before_notify=before_notify_callback)
    assert c.before_notify == before_notify_callback
