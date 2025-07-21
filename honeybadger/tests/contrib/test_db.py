import time
import re
from functools import wraps
from unittest.mock import patch, MagicMock
from honeybadger.tests.utils import with_config

import pytest

from honeybadger import honeybadger
from honeybadger.config import Configuration
from honeybadger.contrib.db import DBHoneybadger


@patch("honeybadger.honeybadger.event")
def test_execute_sends_event_when_enabled(mock_event):
    DBHoneybadger.execute("SELECT 1", start=0)
    mock_event.assert_called_once()
    args, kwargs = mock_event.call_args
    assert args[0] == "db.query"
    assert args[1]["query"] == "SELECT 1"
    assert args[1]["duration"] > 0
    assert "params" not in args[1]


@with_config({"insights_config": {"db": {"disabled": True}}})
@patch("honeybadger.honeybadger.event")
def test_execute_does_not_send_event_when_disabled(mock_event):
    DBHoneybadger.execute("SELECT 1", start=0)
    mock_event.assert_not_called()


@with_config({"insights_config": {"db": {"include_params": True}}})
@patch("honeybadger.honeybadger.event")
def test_execute_includes_params(mock_event):
    params = (123, "abc")
    DBHoneybadger.execute("SELECT x FROM t WHERE a=%s AND b=%s", start=0, params=params)
    args, kwargs = mock_event.call_args
    assert args[1]["params"] == params


@patch("honeybadger.honeybadger.event")
def test_execute_does_not_include_params_when_not_configured(mock_event):
    params = (1, 2)
    DBHoneybadger.execute("SELECT 1", start=0, params=params)
    args, kwargs = mock_event.call_args
    assert "params" not in args[1]


@with_config(
    {"insights_config": {"db": {"exclude_queries": [re.compile(r"SELECT 1")]}}}
)
@patch("honeybadger.honeybadger.event")
def test_execute_excludes_queries_for_regexes(mock_event):
    DBHoneybadger.execute("SELECT 1 abc", start=0)
    mock_event.assert_not_called()


@with_config({"insights_config": {"db": {"exclude_queries": ["PRAGMA"]}}})
@patch("honeybadger.honeybadger.event")
def test_execute_excludes_queries_for_strings(mock_event):
    DBHoneybadger.execute("PRAGMA (*)", start=0)
    mock_event.assert_not_called()
