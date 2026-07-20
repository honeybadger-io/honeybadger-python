import json
import os
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from honeybadger import honeybadger
from honeybadger.contrib.llm import _bridge
from honeybadger.contrib.llm._bridge import (
    CONTEXT_ATTR_PREFIX,
    snapshot_context_attributes,
    export_spans,
)
from honeybadger.tests.contrib.llm_helpers import FakeSpan

import honeybadger.contrib.llm as llm_module
from honeybadger.contrib.llm import LLMHoneybadger, auto_init, CONTENT_ENV_VAR


class RecordingSpan(FakeSpan):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_attributes = {}

    def set_attribute(self, key, value):
        self.set_attributes[key] = value


def owner(active=True):
    return SimpleNamespace(active=active)


def configured(**llm_overrides):
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": llm_overrides},
    )


def chat_span(**attr_overrides):
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 12,
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]
        ),
        "gen_ai.output.messages": json.dumps(
            [{"role": "assistant", "parts": [{"type": "text", "content": "hello"}]}]
        ),
    }
    attrs.update(attr_overrides)
    return FakeSpan(attributes=attrs)


def teardown_function():
    honeybadger.reset_event_context()


# --- context snapshot ---


def test_snapshot_copies_scalar_event_context_onto_span():
    honeybadger.set_event_context(request_id="req-1", user_id=42, nested={"a": 1})
    span = RecordingSpan()
    snapshot_context_attributes(span)
    assert span.set_attributes[CONTEXT_ATTR_PREFIX + "request_id"] == "req-1"
    assert span.set_attributes[CONTEXT_ATTR_PREFIX + "user_id"] == 42
    assert CONTEXT_ATTR_PREFIX + "nested" not in span.set_attributes  # scalars only


# --- export_spans ---


def test_export_emits_llm_chat_event_with_context_lift():
    configured()
    span = chat_span()
    span.attributes[CONTEXT_ATTR_PREFIX + "request_id"] = "req-9"
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([span], owner())
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"
    assert data["provider"] == "openai"
    assert data["request_id"] == "req-9"
    assert "prompts" not in data  # include_prompts defaults off
    assert "response" not in data


def test_export_includes_content_when_opted_in():
    configured(include_prompts=True, include_responses=True)
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner())
    data = mock_event.call_args[0][1]
    assert data["prompts"] == [{"role": "user", "content": "hi"}]
    assert data["response"] == [{"role": "assistant", "content": "hello"}]


def test_export_respects_independent_flags():
    configured(include_prompts=True, include_responses=False)
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner())
    data = mock_event.call_args[0][1]
    assert "prompts" in data and "response" not in data


def test_export_skips_when_disabled_or_inactive_or_not_llm():
    configured(disabled=True)
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner())  # disabled
        mock_event.assert_not_called()
    configured()
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner(active=False))  # torn down
        export_spans([FakeSpan(attributes={"http.method": "GET"})], owner())
        mock_event.assert_not_called()


def test_export_exclude_models_exact_string_and_regex():
    configured(exclude_models=["gpt-4o", re.compile(r"^o1-")])
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner())  # exact
        export_spans(
            [chat_span(**{"gen_ai.request.model": "o1-mini"})], owner()
        )  # regex
        mock_event.assert_not_called()
    with patch.object(honeybadger, "event") as mock_event:
        # substring must NOT match ("gpt-4" is not "gpt-4o")
        export_spans([chat_span(**{"gen_ai.request.model": "gpt-4"})], owner())
        mock_event.assert_called_once()


def test_export_never_raises_on_malformed_span():
    configured()

    class ExplodingSpan:
        @property
        def attributes(self):
            raise RuntimeError("boom")

    with patch.object(honeybadger, "event") as mock_event:
        export_spans([ExplodingSpan(), chat_span()], owner())  # survives, continues
        mock_event.assert_called_once()


# --- LLMHoneybadger shell + auto_init ---


@pytest.fixture(autouse=True)
def reset_llm_state(monkeypatch):
    yield
    if llm_module._active_instance is not None:
        llm_module._active_instance.tearDown()
    llm_module._auto_instance = None
    os.environ.pop(CONTENT_ENV_VAR, None)


def test_module_imports_without_otel():
    # The suite itself may not have otel installed on this row; importing
    # the module and constructing the class must always work.
    instance = LLMHoneybadger()
    assert instance.active is False


def test_init_without_deps_raises_importerror(monkeypatch):
    monkeypatch.setattr(llm_module, "_otel_available", lambda: False)
    with pytest.raises(ImportError) as excinfo:
        LLMHoneybadger().init()
    assert "honeybadger[llm]" in str(excinfo.value)


class _FakeProvider:
    """Minimal stand-in for opentelemetry.sdk.trace.TracerProvider."""

    def __init__(self):
        self.added = []

    def add_span_processor(self, processor):
        self.added.append(processor)

    def shutdown(self):
        self.added.append("shutdown")

    def force_flush(self, timeout_millis=30000):
        return True


def _fake_otel(monkeypatch, instance_holder=None):
    """Stub the otel-touching seams so init() runs without the extra."""
    monkeypatch.setattr(llm_module, "_otel_available", lambda: True)
    fake_provider = _FakeProvider()
    monkeypatch.setattr(llm_module, "_build_provider", lambda: fake_provider)
    monkeypatch.setattr(llm_module, "_attach_pipeline", lambda self, provider: None)
    instrumented = []
    monkeypatch.setattr(
        llm_module,
        "_activate_instrumentors",
        lambda self, provider: instrumented.append("openai") or ["openai"],
    )
    monkeypatch.setattr(llm_module, "_deactivate_instrumentors", lambda self: None)
    return fake_provider, instrumented


def test_init_teardown_lifecycle_and_guard(monkeypatch):
    _fake_otel(monkeypatch)
    first = LLMHoneybadger()
    first.init()
    assert first.active is True
    first.init()  # idempotent
    second = LLMHoneybadger()
    with pytest.raises(RuntimeError):
        second.init()
    first.tearDown()
    assert first.active is False
    second.init()  # guard released
    second.tearDown()


def test_env_gating_set_and_restored(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True}},
    )
    instance = LLMHoneybadger()
    instance.init()
    assert os.environ[CONTENT_ENV_VAR] == "span_only"
    instance.tearDown()
    assert CONTENT_ENV_VAR not in os.environ


def test_env_gating_never_overrides_user(monkeypatch):
    _fake_otel(monkeypatch)
    os.environ[CONTENT_ENV_VAR] = "no_content"
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True}},
    )
    instance = LLMHoneybadger()
    instance.init()
    assert os.environ[CONTENT_ENV_VAR] == "no_content"
    instance.tearDown()
    assert os.environ[CONTENT_ENV_VAR] == "no_content"


def test_env_gating_left_unset_when_content_off(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(
        api_key="fake", insights_enabled=True, insights_config={"llm": {}}
    )
    instance = LLMHoneybadger()
    instance.init()
    assert CONTENT_ENV_VAR not in os.environ
    instance.tearDown()


def test_invalid_export_value_rejected():
    with pytest.raises(ValueError):
        LLMHoneybadger(export="carrier-pigeon")


def test_auto_init_is_silent_without_deps(monkeypatch):
    monkeypatch.setattr(llm_module, "_otel_available", lambda: False)
    assert auto_init() is None  # no raise


def test_auto_init_shares_one_instance(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(api_key="fake", insights_enabled=True)
    first = auto_init()
    second = auto_init()
    assert first is second and first.active


def test_auto_init_respects_disabled(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"disabled": True}},
    )
    assert auto_init() is None
