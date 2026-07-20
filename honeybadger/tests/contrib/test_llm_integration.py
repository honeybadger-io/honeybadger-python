"""End-to-end: real genai-openai instrumentor + openai SDK against a mocked
HTTP transport, into a patched honeybadger.event. Skipped when the [llm]
extra isn't installed (Python < 3.10 rows)."""

import json
import os
from unittest.mock import patch

import pytest

otel = pytest.importorskip("opentelemetry.instrumentation.genai.openai")
openai = pytest.importorskip("openai")
httpx = pytest.importorskip("httpx")

from honeybadger import honeybadger
from honeybadger.contrib.llm import LLMHoneybadger, CONTENT_ENV_VAR

CHAT_RESPONSE = {
    "id": "chatcmpl-test1",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "gpt-4o-2024-08-06",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello there"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
}


@pytest.fixture
def otel_otlp():
    """Gate OTLP-mode tests on the separately-installed exporter package
    without skipping the whole module (openai/otel/httpx are required
    unconditionally above)."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv(CONTENT_ENV_VAR, "span_only")
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True, "include_responses": True}},
    )
    instance = LLMHoneybadger(instruments=["openai"])
    instance.init()
    yield instance
    instance.tearDown()


def openai_client(handler):
    transport = httpx.MockTransport(handler)
    return openai.OpenAI(
        api_key="sk-test", http_client=httpx.Client(transport=transport)
    )


def flush(instance):
    instance._provider.force_flush()


def test_chat_completion_end_to_end(llm):
    client = openai_client(lambda request: httpx.Response(200, json=CHAT_RESPONSE))
    with patch.object(honeybadger, "event") as mock_event:
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
        flush(llm)
    assert mock_event.called
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"
    assert data["model"] == "gpt-4o"
    assert data["input_tokens"] == 9
    assert data["output_tokens"] == 3
    assert data["duration"] >= 0
    # record observed fields for the attribute matrix (Task 11)


def test_error_response_end_to_end(llm):
    client = openai_client(
        lambda request: httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    with patch.object(honeybadger, "event") as mock_event:
        with pytest.raises(openai.RateLimitError):
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
            )
        flush(llm)
    assert mock_event.called
    data = mock_event.call_args[0][1]
    assert "error" in data
    assert "RateLimit" in str(data["error"]) or "429" in str(data["error"])


def test_streaming_with_include_usage(llm):
    chunks = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-4o",
            "choices": [
                {"index": 0, "delta": {"content": "hel"}, "finish_reason": None}
            ],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-4o",
            "choices": [
                {"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}
            ],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-4o",
            "choices": [],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        },
    ]
    body = (
        "".join("data: %s\n\n" % json.dumps(chunk) for chunk in chunks)
        + "data: [DONE]\n\n"
    )
    client = openai_client(
        lambda request: httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    with patch.object(honeybadger, "event") as mock_event:
        stream = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        for _ in stream:
            pass
        flush(llm)
    assert mock_event.called
    data = mock_event.call_args[0][1]
    assert data.get("input_tokens") == 9


def test_teardown_flushes_buffered_spans_without_manual_flush(llm):
    # Regression for silent span drop: tearDown() must flush the owned
    # provider before flipping self._initialized False, since the exporter
    # gates on owner.active (see _bridge._export_one). No force_flush() is
    # called here on purpose -- tearDown() alone must deliver the event.
    client = openai_client(lambda request: httpx.Response(200, json=CHAT_RESPONSE))
    with patch.object(honeybadger, "event") as mock_event:
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
        llm.tearDown()
    assert mock_event.called
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"


def test_teardown_flushes_buffered_spans_for_borrowed_provider(monkeypatch):
    # Regression: the borrowed-provider tearDown() path used to skip
    # flushing entirely (flush only ran for owned providers), silently
    # dropping any span still sitting in the BatchSpanProcessor's buffer.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setenv(CONTENT_ENV_VAR, "span_only")
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True, "include_responses": True}},
    )
    provider = TracerProvider()
    # A second, independent processor/exporter on the same borrowed
    # provider -- proves tearDown() only shuts down OUR processor, not the
    # provider or its other processors.
    other_recorder = _RecordingExporter()
    provider.add_span_processor(SimpleSpanProcessor(other_recorder))

    shutdown_mock = patch.object(provider, "shutdown", wraps=provider.shutdown)
    instance = LLMHoneybadger(instruments=["openai"], tracer_provider=provider)
    instance.init()
    our_worker_thread = instance._processor._batch_processor._worker_thread
    assert our_worker_thread.is_alive()

    client = openai_client(lambda request: httpx.Response(200, json=CHAT_RESPONSE))
    with shutdown_mock as mock_shutdown:
        with patch.object(honeybadger, "event") as mock_event:
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
            )
            # No manual flush -- tearDown() alone must deliver the event.
            instance.tearDown()
        assert mock_event.called
        event_type, data = mock_event.call_args[0]
        assert event_type == "llm.chat"
        # Borrowed provider must never be shut down -- it's the app's.
        mock_shutdown.assert_not_called()

        # Regression (finding 6): OUR processor's batch-worker thread must
        # actually stop on tearDown() -- otherwise repeated init/tearDown
        # against one long-lived borrowed provider accumulates live daemon
        # threads forever.
        our_worker_thread.join(timeout=5)
        assert not our_worker_thread.is_alive()

        # The provider itself, and its OTHER processor, still work fine --
        # only our processor was shut down.
        spans_before = len(other_recorder.spans)
        with tracer_span_on(provider):
            pass
        provider.force_flush()
        assert len(other_recorder.spans) == spans_before + 1
        assert other_recorder.spans[-1].name == "probe"

        # Inertness: spans ended after tearDown() must not emit through OUR
        # (still physically attached -- no remove_span_processor API)
        # pipeline. The openai instrumentor itself is uninstrumented by
        # tearDown(), so drive the exporter pipeline directly via the
        # borrowed provider's tracer.
        with patch.object(honeybadger, "event") as mock_event_after:
            tracer = provider.get_tracer("test-inertness")
            with tracer.start_as_current_span("chat gpt-4o") as span:
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("gen_ai.request.model", "gpt-4o")
            provider.force_flush()
        assert not mock_event_after.called

    provider.shutdown()


def tracer_span_on(provider):
    tracer = provider.get_tracer("test-other-processor")
    return tracer.start_as_current_span("probe")


class _RecordingExporter:
    """Stand-in for the real OTLPSpanExporter: records what it's handed
    instead of making network calls."""

    def __init__(self):
        self.spans = []

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class _Owner:
    active = True


def _otlp_recorder_setup(**llm_config_overrides):
    """Real TracerProvider + real make_otlp_exporter(owner, wrapped=recorder),
    synchronous SimpleSpanProcessor so we don't need to sleep/flush threads."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from honeybadger.contrib.llm import _bridge

    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": llm_config_overrides},
    )
    recorder = _RecordingExporter()
    exporter = _bridge.make_otlp_exporter(_Owner(), wrapped=recorder)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, recorder


def _make_content_span(
    tracer, prompt="secret prompt", model="gpt-4o", name="chat gpt-4o", extra=None
):
    message = {"role": "user", "parts": [{"type": "text", "content": prompt}]}
    if extra:
        message.update(extra)
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.input.messages", json.dumps([message]))
        span.set_attribute("gen_ai.system_instructions", json.dumps("be helpful"))
    return span


def test_otlp_exporter_drops_content_attrs_by_default(otel_otlp):
    provider, recorder = _otlp_recorder_setup()
    tracer = provider.get_tracer("test-otlp")
    original = _make_content_span(tracer)
    provider.force_flush()

    assert len(recorder.spans) == 1
    cloned = recorder.spans[0]
    assert "gen_ai.input.messages" not in cloned.attributes
    assert "gen_ai.system_instructions" not in cloned.attributes
    assert cloned.attributes["gen_ai.request.model"] == "gpt-4o"

    # (d) original span's attributes must not be mutated by scrub+clone.
    assert "gen_ai.input.messages" in original.attributes
    assert "secret prompt" in original.attributes["gen_ai.input.messages"]


def test_otlp_exporter_keeps_and_redacts_content_when_opted_in(otel_otlp):
    provider, recorder = _otlp_recorder_setup(include_prompts=True)
    honeybadger.configure(params_filters=["password"])
    tracer = provider.get_tracer("test-otlp")
    _make_content_span(tracer, extra={"password": "hunter2"})
    provider.force_flush()

    assert len(recorder.spans) == 1
    cloned = recorder.spans[0]
    # Content attr survives (not dropped, unlike the default case)...
    assert "gen_ai.input.messages" in cloned.attributes
    decoded = json.loads(cloned.attributes["gen_ai.input.messages"])
    # ...but is redacted per params_filters (structural redaction by key).
    assert decoded[0]["password"] == "[FILTERED]"
    # ...and the real nested `parts` shape is flattened to `content` before
    # the content policy runs, so truncation/part-dropping actually apply
    # (regression: it used to pass through unprocessed under "parts").
    assert "parts" not in decoded[0]
    assert decoded[0]["content"] == "secret prompt"


def test_otlp_exporter_drops_excluded_model_spans(otel_otlp):
    provider, recorder = _otlp_recorder_setup(exclude_models=["gpt-4o"])
    tracer = provider.get_tracer("test-otlp")
    _make_content_span(tracer, model="gpt-4o")
    _make_content_span(tracer, model="gpt-3.5-turbo")
    provider.force_flush()

    models = [s.attributes["gen_ai.request.model"] for s in recorder.spans]
    assert "gpt-4o" not in models
    assert "gpt-3.5-turbo" in models


def test_otlp_exporter_clone_preserves_span_shape(otel_otlp):
    provider, recorder = _otlp_recorder_setup()
    tracer = provider.get_tracer("test-otlp")
    original = _make_content_span(tracer)
    provider.force_flush()

    assert len(recorder.spans) == 1
    cloned = recorder.spans[0]
    assert cloned.name == original.name
    assert cloned.kind == original.kind
    assert cloned.status.status_code == original.status.status_code
    assert cloned.start_time == original.start_time
    assert cloned.end_time == original.end_time


def test_early_terminated_stream_still_emits(llm):
    chunks = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}],
        },
    ] * 5
    body = (
        "".join("data: %s\n\n" % json.dumps(chunk) for chunk in chunks)
        + "data: [DONE]\n\n"
    )
    client = openai_client(
        lambda request: httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    with patch.object(honeybadger, "event") as mock_event:
        stream = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        for i, _ in enumerate(stream):
            if i == 1:
                stream.close()
                break
        flush(llm)
    # We emit whatever span arrives (spec: partial output, missing usage OK).
    assert mock_event.called
    event_type, _data = mock_event.call_args[0]
    assert event_type == "llm.chat"


def test_exception_mid_stream_consumption_still_emits(llm):
    # Consumer code raising mid-loop (not calling .close()/break itself) is
    # a realistic failure mode -- e.g. a callback that processes each chunk
    # throws. Use `with stream:` (openai.Stream supports the context
    # manager protocol) so the underlying response/span is deterministically
    # closed on exception propagation, not left to GC (that unconsumed-
    # generator scenario is deliberately not tested here -- see the plan's
    # deferral list).
    chunks = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}],
        },
    ] * 5
    body = (
        "".join("data: %s\n\n" % json.dumps(chunk) for chunk in chunks)
        + "data: [DONE]\n\n"
    )
    client = openai_client(
        lambda request: httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    with patch.object(honeybadger, "event") as mock_event:
        try:
            with client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            ) as stream:
                for i, _ in enumerate(stream):
                    if i == 0:
                        raise ValueError("boom")
        except ValueError:
            pass
        flush(llm)
    assert mock_event.called
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"
    # Unlike the deliberate-close early-terminated-stream case (no `error`
    # field), an exception propagating out of the consumer loop is recorded:
    # confirmed empirically, see contrib/llm.md attribute matrix notes.
    assert data.get("error") == "ValueError"


def test_env_gating_value_enables_real_content_capture(monkeypatch):
    """Production gating path: CONTENT_ENV_VAR is left UNSET (never hand-set
    here, unlike the `llm` fixture) so _apply_env_gating() must write it
    itself from include_prompts=True. Then drive a real OpenAI instrumentor
    call and assert the prompt text actually reaches the emitted event.

    This proves the exact value _apply_env_gating() writes ("span_only") is
    honored by the real instrumentor -- not just parsed by our own fake-otel
    unit tests. See opentelemetry/util/genai/utils.py:20-28
    get_content_capturing_mode(), which upper-cases the env value before an
    enum lookup, so "span_only" and "SPAN_ONLY" are equivalent for both the
    openai and anthropic instrumentors (they share this parse site via
    opentelemetry.util.genai.handler.TelemetryHandler)."""
    monkeypatch.delenv(CONTENT_ENV_VAR, raising=False)
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True}},
    )
    instance = LLMHoneybadger(instruments=["openai"])
    instance.init()
    try:
        assert os.environ[CONTENT_ENV_VAR] == "span_only"
        client = openai_client(
            lambda request: httpx.Response(200, json=CHAT_RESPONSE)
        )
        with patch.object(honeybadger, "event") as mock_event:
            client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )
            flush(instance)
        assert mock_event.called
        data = mock_event.call_args[0][1]
        assert "prompts" in data
        assert any(m.get("content") == "hi" for m in data["prompts"])
    finally:
        instance.tearDown()


def test_async_client_chat_completion_end_to_end(llm):
    import asyncio

    async def _run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=CHAT_RESPONSE)
        )
        client = openai.AsyncOpenAI(
            api_key="sk-test", http_client=httpx.AsyncClient(transport=transport)
        )
        await client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    with patch.object(honeybadger, "event") as mock_event:
        asyncio.run(_run())
        flush(llm)
    assert mock_event.called
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"
    assert data["model"] == "gpt-4o"
