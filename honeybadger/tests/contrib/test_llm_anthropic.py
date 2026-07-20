"""End-to-end: real genai-anthropic instrumentor + anthropic SDK against a
mocked HTTP transport. Asserts both raw span attributes (RecordingProcessor)
and emitted events. Skipped when packages aren't installed."""
import asyncio
import json
from unittest.mock import patch

import pytest

otel_anthropic = pytest.importorskip("opentelemetry.instrumentation.genai.anthropic")
anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx")

from honeybadger import honeybadger
from honeybadger.contrib.llm import LLMHoneybadger, CONTENT_ENV_VAR
from honeybadger.tests.contrib.llm_recording import RecordingProcessor

MESSAGES_RESPONSE = {
    "id": "msg_test1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "hello there"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 9,
        "output_tokens": 3,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 2,
    },
}


@pytest.fixture
def llm(monkeypatch):
    # PRODUCTION gating path: env var unset; _apply_env_gating must set the
    # value Task 2 standardized, and the instrumentor must honor it.
    monkeypatch.delenv(CONTENT_ENV_VAR, raising=False)
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True, "include_responses": True}},
    )
    instance = LLMHoneybadger(instruments=["anthropic"])
    instance.init()
    recorder = RecordingProcessor()
    instance._provider.add_span_processor(recorder)
    instance.recorder = recorder
    yield instance
    instance.tearDown()


def anthropic_client(handler):
    transport = httpx.MockTransport(handler)
    return anthropic.Anthropic(
        api_key="sk-ant-test", http_client=httpx.Client(transport=transport)
    )


def flush(instance):
    instance._provider.force_flush()


def test_messages_create_end_to_end(llm):
    client = anthropic_client(lambda request: httpx.Response(200, json=MESSAGES_RESPONSE))
    with patch.object(honeybadger, "event") as mock_event:
        client.messages.create(
            model="claude-sonnet-4-5", max_tokens=64,
            system="be brief",
            messages=[{"role": "user", "content": "hi"}],
        )
        flush(llm)
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"
    assert data["provider"] == "anthropic"
    assert data["model"] == "claude-sonnet-4-5"
    # OBSERVED (genai-anthropic 1.0b0, messages_extractors.extract_usage_tokens):
    # gen_ai.usage.input_tokens is NOT the raw API's usage.input_tokens (9);
    # the instrumentor sums base + cache_creation + cache_read into a single
    # "total input tokens" figure: 9 + 2 (cache_creation) + 7 (cache_read) = 18.
    # This is upstream instrumentor behavior, not an adapter gap -- _semconv.py
    # just copies the attribute through verbatim.
    assert data["input_tokens"] == 18
    assert data["output_tokens"] == 3
    assert data["duration"] >= 0
    # cache tokens: assert against raw span first, then event mapping
    raw = llm.recorder.spans[-1].attributes
    if "gen_ai.usage.cache_read.input_tokens" in raw:
        assert data["cache_read_tokens"] == 7
        assert data["cache_creation_tokens"] == 2
    # OBSERVED: stop_reason "end_turn" -> finish_reason "stop"
    # (utils.normalize_finish_reason maps end_turn/stop_sequence -> "stop",
    # max_tokens -> "length", tool_use -> "tool_calls"; anything else passes
    # through unchanged). Raw span carries the *mapped* value only --
    # gen_ai.response.finish_reasons == ("stop",) -- the original Anthropic
    # stop_reason string never reaches the span.
    assert raw["gen_ai.response.finish_reasons"] == ("stop",)
    assert data["finish_reason"] == "stop"
    # content via the production gating path
    assert data.get("prompts"), "prompts expected (production env-gating path)"
    assert data.get("response")


def test_stop_sequence_finish_reason_collapses_with_end_turn(llm):
    # OBSERVED: a response that stopped because it hit a configured stop
    # sequence (stop_reason="stop_sequence") normalizes to the SAME
    # finish_reason ("stop") as a natural end_turn completion. The actual
    # matched stop-sequence string (response-side `stop_sequence` field on
    # the Anthropic Message) is never captured by the instrumentor at all --
    # neither on the raw span nor in the event (upstream omission, not an
    # adapter gap: there is no gen_ai.response.stop_sequence attribute to
    # adapt). Only the *request's* configured stop_sequences list is
    # captured, via gen_ai.request.stop_sequences.
    response = {
        **MESSAGES_RESPONSE,
        "stop_reason": "stop_sequence",
        "stop_sequence": "STOP-NOW",
    }
    client = anthropic_client(lambda request: httpx.Response(200, json=response))
    with patch.object(honeybadger, "event") as mock_event:
        client.messages.create(
            model="claude-sonnet-4-5", max_tokens=64,
            stop_sequences=["STOP-NOW"],
            messages=[{"role": "user", "content": "hi"}],
        )
        flush(llm)
    data = mock_event.call_args[0][1]
    raw = llm.recorder.spans[-1].attributes
    assert raw["gen_ai.request.stop_sequences"] == ("STOP-NOW",)
    assert "gen_ai.response.stop_sequence" not in raw
    assert raw["gen_ai.response.finish_reasons"] == ("stop",)
    assert data["finish_reason"] == "stop"


def test_request_params_adapter_gaps(llm):
    # ADAPTER GAP (for Task 4, honeybadger/contrib/llm/_semconv.py): the
    # instrumentor puts gen_ai.request.max_tokens, .stop_sequences, .top_k,
    # and .top_p on the raw span, but _semconv._SCALAR_FIELDS only maps
    # gen_ai.request.temperature -> "temperature" among request params --
    # the other four never reach the emitted event. Do NOT fix _semconv.py
    # here (out of scope for this task); this test only records the gap.
    client = anthropic_client(lambda request: httpx.Response(200, json=MESSAGES_RESPONSE))
    with patch.object(honeybadger, "event") as mock_event:
        client.messages.create(
            model="claude-sonnet-4-5", max_tokens=64,
            temperature=0.7, top_p=0.9, top_k=40,
            stop_sequences=["X"],
            messages=[{"role": "user", "content": "hi"}],
        )
        flush(llm)
    data = mock_event.call_args[0][1]
    raw = llm.recorder.spans[-1].attributes
    # Raw span has all four...
    assert raw["gen_ai.request.max_tokens"] == 64
    assert raw["gen_ai.request.stop_sequences"] == ("X",)
    assert raw["gen_ai.request.top_k"] == 40
    assert raw["gen_ai.request.top_p"] == 0.9
    # temperature IS mapped (control: proves the gap is field-specific, not
    # "request params are dropped wholesale").
    assert data["temperature"] == 0.7
    # ...but only temperature reaches the event -- these four are absent:
    assert "max_tokens" not in data
    assert "stop_sequences" not in data
    assert "top_k" not in data
    assert "top_p" not in data


def test_error_response_end_to_end(llm):
    client = anthropic_client(
        lambda request: httpx.Response(
            429, json={"type": "error",
                       "error": {"type": "rate_limit_error", "message": "slow down"}}
        )
    )
    with patch.object(honeybadger, "event") as mock_event:
        with pytest.raises(anthropic.RateLimitError):
            client.messages.create(
                model="claude-sonnet-4-5", max_tokens=64,
                messages=[{"role": "user", "content": "hi"}],
            )
        flush(llm)
    data = mock_event.call_args[0][1]
    assert data.get("error"), "expected non-empty error field"
    assert data["error"] == "RateLimitError"
    raw = llm.recorder.spans[-1].attributes
    assert raw["error.type"] == "RateLimitError"
    # No usage/output attributes on a failed call (request never completed).
    assert "gen_ai.usage.input_tokens" not in raw
    assert "gen_ai.output.messages" not in raw


def _stream_body():
    # Complete per the SDK accumulator: message_start snapshot needs
    # stop_sequence and usage; message_delta carries stop_reason AND
    # stop_sequence. Cross-check against the installed SDK's streaming
    # accumulator (anthropic/lib/streaming/_messages.py) before trusting.
    start_message = {
        **MESSAGES_RESPONSE,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 9, "output_tokens": 0},
    }
    events = [
        {"type": "message_start", "message": start_message},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta",
         "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]
    return "".join(
        "event: %s\ndata: %s\n\n" % (e["type"], json.dumps(e)) for e in events
    ).encode()


def _sse_client(body):
    return anthropic_client(
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )
    )


def test_streaming_end_to_end(llm):
    # OBSERVED (significant, genai-anthropic 1.0b0): consuming via
    # `stream.text_stream` -- the SDK's advertised convenience API, and what
    # this scenario uses -- NEVER populates usage/output/finish_reason on the
    # raw span, even when the stream is read to full completion. Root cause:
    # `MessageStream.text_stream` is a generator bound at __init__ time to
    # `self.__stream_text__()`, which internally does `for chunk in self:`
    # against the UNWRAPPED anthropic SDK object. The OTel
    # `MessagesStreamWrapper` (opentelemetry/instrumentation/genai/anthropic/
    # wrappers.py) is a `wrapt.ObjectProxy` around that same MessageStream;
    # `wrapper.text_stream` forwards (via ObjectProxy.__getattr__) to that
    # already-bound generator, so iterating it never touches
    # `SyncStreamWrapper.__next__` / `_process_chunk`
    # (opentelemetry/util/genai/stream.py) -- the hook that accumulates the
    # message snapshot the instrumentor needs. Only the request-side
    # attributes (set before the call) land on the span; `_stop()` still
    # runs on `__exit__` (so the span always ends and the event still
    # fires), but `_set_response_attributes(invocation, None, ...)` is a
    # no-op because `self._self_message` was never populated.
    # See test_streaming_direct_iteration_populates_response_data below for
    # the control case (iterating the wrapper itself DOES populate these).
    client = _sse_client(_stream_body())
    with patch.object(honeybadger, "event") as mock_event:
        with client.messages.stream(
            model="claude-sonnet-4-5", max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            for _ in stream.text_stream:
                pass
        flush(llm)
    assert mock_event.called
    data = mock_event.call_args[0][1]
    assert data.get("model") == "claude-sonnet-4-5"
    raw = llm.recorder.spans[-1].attributes
    # Upstream omission (not an adapter gap): absent from the raw span too.
    assert "gen_ai.usage.output_tokens" not in raw
    assert "gen_ai.output.messages" not in raw
    assert "gen_ai.response.finish_reasons" not in raw
    assert "output_tokens" not in data
    assert "finish_reason" not in data


def test_streaming_direct_iteration_populates_response_data(llm):
    # Control case for the text_stream finding above: iterating the
    # MessagesStreamWrapper itself (`for chunk in stream:`, matching how
    # opentelemetry.util.genai.stream.SyncStreamWrapper expects to be
    # consumed) DOES drive `_process_chunk` per chunk, so usage/output/
    # finish_reason land on the raw span and reach the event normally.
    client = _sse_client(_stream_body())
    with patch.object(honeybadger, "event") as mock_event:
        with client.messages.stream(
            model="claude-sonnet-4-5", max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            for _ in stream:
                pass
        flush(llm)
    assert mock_event.called
    data = mock_event.call_args[0][1]
    raw = llm.recorder.spans[-1].attributes
    assert raw["gen_ai.usage.input_tokens"] == 9
    assert raw["gen_ai.usage.output_tokens"] == 2
    assert raw["gen_ai.response.finish_reasons"] == ("stop",)
    assert data["input_tokens"] == 9
    assert data["output_tokens"] == 2
    assert data["finish_reason"] == "stop"
    assert data.get("response") == [{"role": "assistant", "content": "hello"}]


def test_streaming_early_close_still_emits(llm):
    # Also via text_stream (see test_streaming_end_to_end), so -- on top of
    # being an early close -- this never gets a populated message snapshot
    # either; the span still ends and the event still fires (that's what
    # this test guards), just with no usage/output data. Event still carries
    # request-side fields (provider/model/prompts).
    client = _sse_client(_stream_body())
    with patch.object(honeybadger, "event") as mock_event:
        with client.messages.stream(
            model="claude-sonnet-4-5", max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            next(iter(stream.text_stream), None)  # consume one chunk, then close
        flush(llm)
    assert mock_event.called
    data = mock_event.call_args[0][1]
    assert data.get("model") == "claude-sonnet-4-5"
    assert "error" not in data  # deliberate close, not a failure


def test_streaming_consumer_exception_still_emits(llm):
    client = _sse_client(_stream_body())
    with patch.object(honeybadger, "event") as mock_event:
        with pytest.raises(ValueError):
            with client.messages.stream(
                model="claude-sonnet-4-5", max_tokens=64,
                messages=[{"role": "user", "content": "hi"}],
            ) as stream:
                for _ in stream.text_stream:
                    raise ValueError("consumer blew up")
        flush(llm)
    assert mock_event.called
    data = mock_event.call_args[0][1]
    # Unlike the deliberate-close case, an exception propagating out of the
    # consumer loop is recorded as an error (matches openai suite's
    # equivalent finding).
    assert data.get("error") == "ValueError"


def test_async_create_end_to_end(llm):
    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=MESSAGES_RESPONSE)
        )
        client = anthropic.AsyncAnthropic(
            api_key="sk-ant-test",
            http_client=httpx.AsyncClient(transport=transport),
        )
        await client.messages.create(
            model="claude-sonnet-4-5", max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        )

    with patch.object(honeybadger, "event") as mock_event:
        asyncio.run(run())
        flush(llm)
    assert mock_event.called
    data = mock_event.call_args[0][1]
    assert data["provider"] == "anthropic"
    # Async path exhibits the same cache-token-summing behavior as sync
    # (same messages_extractors.extract_usage_tokens code path).
    assert data["input_tokens"] == 18
    assert data["cache_read_tokens"] == 7
    assert data["cache_creation_tokens"] == 2
