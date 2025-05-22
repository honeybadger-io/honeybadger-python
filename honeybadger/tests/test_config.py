from __future__ import print_function

import os
import pytest
import logging

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


def test_can_only_set_valid_options(caplog):
    with caplog.at_level(logging.WARNING):
        try:
            Configuration(foo="bar")
        except AttributeError:
            pass
    assert any(
        "Unknown Configuration option" in msg for msg in caplog.text.splitlines()
    )


def test_is_okay_with_unknown_env_var():
    os.environ["HONEYBADGER_FOO"] = "bar"
    try:
        Configuration()
    except Exception:
        pytest.fail("This should fail silently.")


def test_nested_dataclass_raises_for_invalid_key(caplog):
    c = Configuration(insights_config={})
    with caplog.at_level(logging.WARNING):
        c.set_config_from_dict({"insights_config": {"db": {"bogus": True}}})
    assert any("Unknown DBConfig option" in msg for msg in caplog.text.splitlines())


def test_set_config_from_dict_raises_for_unknown_key(caplog):
    c = Configuration()
    with caplog.at_level(logging.WARNING):
        c.set_config_from_dict({"does_not_exist": 123})
    assert any(
        "Unknown Configuration option" in msg for msg in caplog.text.splitlines()
    )


def test_valid_dev_environments():
    valid_dev_environments = ["development", "dev", "test"]

    assert len(Configuration.DEVELOPMENT_ENVIRONMENTS) == len(valid_dev_environments)
    assert set(Configuration.DEVELOPMENT_ENVIRONMENTS) == set(valid_dev_environments)


def test_is_dev_true_for_dev_environments():
    for env in Configuration.DEVELOPMENT_ENVIRONMENTS:
        c = Configuration(environment=env)
        assert c.is_dev()


def test_is_dev_false_for_non_dev_environments():
    c = Configuration(environment="production")
    assert c.is_dev() == False


def test_force_report_data_not_active():
    c = Configuration()
    assert c.force_report_data == False


def test_configure_before_notify():
    def before_notify_callback(notice):
        return notice

    c = Configuration(before_notify=before_notify_callback)
    assert c.before_notify == before_notify_callback


def test_configure_nested_insights_config():
    c = Configuration(insights_config={"db": {"disabled": True}})
    assert c.insights_config.db.disabled == True


def test_configure_throws_for_invalid_insights_config(caplog):
    with caplog.at_level(logging.WARNING):
        Configuration(insights_config={"foo": "bar"})
    assert any(
        "Unknown InsightsConfig option" in msg for msg in caplog.text.splitlines()
    )


def test_configure_merges_insights_config():
    c = Configuration(api_key="test", insights_config={})

    c.set_config_from_dict({"insights_config": {"db": {"include_params": True}}})
    assert hasattr(c.insights_config, "db")
    assert c.insights_config.db.include_params is True

    c.set_config_from_dict({"insights_config": {"celery": {"disabled": True}}})
    assert hasattr(c.insights_config, "celery")
    assert c.insights_config.celery.disabled is True

    assert c.insights_config.db.include_params is True
