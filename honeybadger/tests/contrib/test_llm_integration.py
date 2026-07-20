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
import honeybadger.contrib.llm as llm_module
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

    monkeypatch.setenv(CONTENT_ENV_VAR, "span_only")
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True, "include_responses": True}},
    )
    provider = TracerProvider()
    shutdown_mock = patch.object(provider, "shutdown", wraps=provider.shutdown)
    instance = LLMHoneybadger(instruments=["openai"], tracer_provider=provider)
    instance.init()
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

        # Inertness: spans ended after tearDown() must not emit. The openai
        # instrumentor itself is uninstrumented by tearDown(), so drive the
        # (still physically attached -- no remove_span_processor API)
        # exporter pipeline directly via the borrowed provider's tracer.
        with patch.object(honeybadger, "event") as mock_event_after:
            tracer = provider.get_tracer("test-inertness")
            with tracer.start_as_current_span("chat gpt-4o") as span:
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("gen_ai.request.model", "gpt-4o")
            provider.force_flush()
        assert not mock_event_after.called

    provider.shutdown()


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
