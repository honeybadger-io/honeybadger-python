import json
import os
import re
import threading
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


def test_export_budget_accounts_for_lifted_context():
    # Regression: lifted honeybadger.context.* attributes used to be merged
    # AFTER enforce_event_budget() ran, so a large context value could push
    # a "budgeted" event back over max_event_bytes. Lifting must happen
    # first so the budget check sees the full, final payload.
    configured(include_prompts=True, include_responses=True, max_event_bytes=200)
    span = chat_span()
    span.attributes[CONTEXT_ATTR_PREFIX + "session_id"] = "s" * 300
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([span], owner())
    data = mock_event.call_args[0][1]
    assert data["session_id"] == "s" * 300  # context still present
    assert data["content_dropped"] is True  # content sacrificed to fit budget
    assert "prompts" not in data
    assert "response" not in data


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


def test_otel_available_handles_missing_parent_package(monkeypatch):
    # Regression: find_spec("opentelemetry.sdk") RAISES ModuleNotFoundError
    # (rather than returning None) when the "opentelemetry" package itself
    # isn't installed at all -- the common core-only install / Python 3.9
    # path. _otel_available() must treat that the same as "not found" so
    # init() raises the documented [llm]-extra ImportError, not a bare
    # "No module named 'opentelemetry'".
    import importlib.util as ilu

    real_find_spec = ilu.find_spec

    def boom(name, *a, **kw):
        if name.startswith("opentelemetry"):
            raise ModuleNotFoundError("No module named 'opentelemetry'")
        return real_find_spec(name, *a, **kw)

    monkeypatch.setattr(ilu, "find_spec", boom)
    assert llm_module._otel_available() is False
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


def test_teardown_raises_while_init_in_progress_on_another_thread(monkeypatch):
    # Regression: tearDown() used to proceed whenever _active_instance is
    # self, even if init() was still mid-flight on another thread (between
    # reserving _active_instance and setting self._initialized = True).
    # That let a concurrent tearDown() clear _active_instance out from under
    # the in-flight init(), defeating the single-instance guard.
    monkeypatch.setattr(llm_module, "_otel_available", lambda: True)
    fake_provider = _FakeProvider()
    monkeypatch.setattr(llm_module, "_build_provider", lambda: fake_provider)
    monkeypatch.setattr(llm_module, "_activate_instrumentors", lambda self, p: [])
    monkeypatch.setattr(llm_module, "_deactivate_instrumentors", lambda self: None)

    entered = threading.Event()
    proceed = threading.Event()

    def blocking_attach(self, provider):
        entered.set()
        assert proceed.wait(timeout=5), "test deadlocked waiting to unblock init()"

    monkeypatch.setattr(llm_module, "_attach_pipeline", blocking_attach)

    instance = LLMHoneybadger()
    init_thread = threading.Thread(target=instance.init)
    init_thread.start()
    try:
        assert entered.wait(timeout=5), "init() never reached _attach_pipeline"
        # init() has reserved _active_instance but hasn't finished yet.
        with pytest.raises(RuntimeError, match="init in progress"):
            instance.tearDown()
    finally:
        proceed.set()
        init_thread.join(timeout=5)

    assert instance.active is True
    assert llm_module._active_instance is instance
    instance.tearDown()
    assert instance.active is False
    assert llm_module._active_instance is None


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


def test_auto_init_recovers_after_user_instance_teardown(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(
        api_key="fake", insights_enabled=True, insights_config={"llm": {}}
    )
    user = LLMHoneybadger()
    user.init()
    assert auto_init() is None  # a user instance is active; auto_init defers
    user.tearDown()
    shared = auto_init()
    assert shared is not None
    assert shared.active is True
    assert auto_init() is shared  # still shared, not permanently disabled


def test_init_failure_cleans_up_owned_provider(monkeypatch):
    fake_provider, _ = _fake_otel(monkeypatch)

    def _boom(self, provider):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_module, "_attach_pipeline", _boom)
    instance = LLMHoneybadger()
    with pytest.raises(RuntimeError):
        instance.init()
    assert "shutdown" in fake_provider.added  # owned provider was cleaned up

    # Guard released: a fresh instance can init() afterwards.
    monkeypatch.setattr(llm_module, "_attach_pipeline", lambda self, provider: None)
    other = LLMHoneybadger()
    other.init()
    assert other.active is True
    other.tearDown()


def test_auto_init_thread_safety(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(
        api_key="fake", insights_enabled=True, insights_config={"llm": {}}
    )

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads

    def worker(i):
        barrier.wait()
        results[i] = auto_init()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    non_none = [r for r in results if r is not None]
    assert len(non_none) > 0
    shared = non_none[0]
    assert all(r is shared for r in non_none)
    assert shared.active is True
    assert llm_module._active_instance is shared

    # Every thread that got None must be recoverable: auto_init() still
    # returns the one shared instance afterward, not permanently disabled.
    final = auto_init()
    assert final is shared


# --- framework auto-init wiring ---


def test_django_middleware_calls_auto_init(monkeypatch):
    # Follows the established Django test bootstrap in test_django.py:
    # bare settings.configure() (idempotent), then override_settings for
    # the specific HONEYBADGER config this test needs.
    from django.conf import settings as django_settings

    try:
        django_settings.configure()
    except RuntimeError:
        pass
    from django.test import override_settings
    from honeybadger.contrib.django import DjangoHoneybadgerMiddleware

    calls = []
    monkeypatch.setattr(llm_module, "auto_init", lambda: calls.append(1))

    with override_settings(
        HONEYBADGER={"INSIGHTS_ENABLED": True, "INSIGHTS_CONFIG": {}}
    ):
        with patch.object(DjangoHoneybadgerMiddleware, "_patch_cursor"):
            DjangoHoneybadgerMiddleware(get_response=lambda request: None)

    assert calls == [1]


# --- scrubbing OTLP exporter ---

from honeybadger.contrib.llm._bridge import scrub_attributes
from honeybadger.config import LLMConfig


def test_scrub_drops_content_attrs_by_default():
    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.input.messages": json.dumps([{"role": "user", "parts": []}]),
        "gen_ai.output.messages": json.dumps([{"role": "assistant", "parts": []}]),
    }
    result = scrub_attributes(attrs, LLMConfig(), ["password"])
    assert "gen_ai.input.messages" not in result
    assert "gen_ai.output.messages" not in result
    assert result["gen_ai.request.model"] == "gpt-4o"
    assert attrs["gen_ai.input.messages"]  # input untouched


def test_scrub_keeps_and_redacts_content_when_opted_in():
    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "content": "hi", "password": "hunter2"}]
        ),
    }
    config = LLMConfig(include_prompts=True)
    result = scrub_attributes(attrs, config, ["password"])
    decoded = json.loads(result["gen_ai.input.messages"])
    assert decoded[0]["password"] == "[FILTERED]"


def test_scrub_returns_none_for_excluded_or_disabled():
    attrs = {"gen_ai.request.model": "gpt-4o"}
    assert scrub_attributes(attrs, LLMConfig(exclude_models=["gpt-4o"]), []) is None
    assert scrub_attributes(attrs, LLMConfig(disabled=True), []) is None


def test_scrub_unparseable_content_is_json_encoded():
    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.input.messages": "{not json",
    }
    config = LLMConfig(include_prompts=True)
    result = scrub_attributes(attrs, config, [])
    assert (
        json.loads(result["gen_ai.input.messages"]) == "[unparseable content removed]"
    )


def test_scrub_gates_system_instructions_with_prompts():
    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.system_instructions": json.dumps(
            [{"type": "text", "content": "be brief"}]
        ),
    }
    # Default config: system_instructions should be dropped
    result = scrub_attributes(attrs, LLMConfig(), [])
    assert "gen_ai.system_instructions" not in result

    # With include_prompts=True: system_instructions should be kept
    result = scrub_attributes(attrs, LLMConfig(include_prompts=True), [])
    assert "gen_ai.system_instructions" in result
    decoded = json.loads(result["gen_ai.system_instructions"])
    assert decoded == [{"type": "text", "content": "be brief"}]


def test_scrub_drops_tool_definitions_by_default():
    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.tool.definitions": json.dumps(
            [{"type": "function", "name": "get_weather", "description": "..."}]
        ),
    }
    result = scrub_attributes(attrs, LLMConfig(), [])
    assert "gen_ai.tool.definitions" not in result
    assert attrs["gen_ai.tool.definitions"]  # input untouched


def test_scrub_keeps_and_redacts_tool_definitions_when_opted_in():
    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.tool.definitions": json.dumps(
            [
                {
                    "type": "function",
                    "name": "get_weather",
                    "description": "...",
                    "api_key": "hunter2",
                }
            ]
        ),
    }
    result = scrub_attributes(attrs, LLMConfig(include_prompts=True), ["api_key"])
    assert "gen_ai.tool.definitions" in result
    decoded = json.loads(result["gen_ai.tool.definitions"])
    assert decoded[0]["api_key"] == "[FILTERED]"
    assert decoded[0]["name"] == "get_weather"


def test_scrub_flattens_nested_parts_and_truncates():
    from honeybadger.contrib.llm._policy import TRUNCATION_MARKER

    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": "x" * 50}]}]
        ),
    }
    config = LLMConfig(include_prompts=True, max_content_length=10)
    result = scrub_attributes(attrs, config, [])
    decoded = json.loads(result["gen_ai.input.messages"])
    assert "parts" not in decoded[0]
    assert decoded[0]["content"] == "x" * 10 + TRUNCATION_MARKER
    assert decoded[0]["role"] == "user"


def test_scrub_flattens_nested_parts_omits_non_text():
    from honeybadger.contrib.llm._policy import OMITTED_PART

    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.input.messages": json.dumps(
            [
                {
                    "role": "user",
                    "parts": [
                        {"type": "text", "content": "hi"},
                        {"type": "blob", "data": "AAAA"},
                    ],
                }
            ]
        ),
    }
    config = LLMConfig(include_prompts=True)
    result = scrub_attributes(attrs, config, [])
    decoded = json.loads(result["gen_ai.input.messages"])
    assert "parts" not in decoded[0]
    assert decoded[0]["content"] == ["hi", OMITTED_PART]


def test_otlp_exporter_requires_package(monkeypatch):
    import importlib.util as ilu

    real_find_spec = ilu.find_spec
    monkeypatch.setattr(
        ilu,
        "find_spec",
        lambda name, *a: None if "exporter" in name else real_find_spec(name, *a),
    )
    with pytest.raises(ImportError) as excinfo:
        _bridge.make_otlp_exporter(owner())
    assert "opentelemetry-exporter-otlp-proto-http" in str(excinfo.value)
