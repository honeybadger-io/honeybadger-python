"""End-to-end: real BotocoreInstrumentor + a stubbed boto3 bedrock-runtime
client. Asserts both raw span attributes (RecordingProcessor) and emitted
events. Skipped when packages aren't installed.

DECISION: metadata-only Bedrock integration (Task 5 of the phase-2 plan).

OBSERVED (opentelemetry-instrumentation-botocore==0.64b0, real converse()
and invoke_model() calls against a Stubber, both with and without
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT set to Task 2's
standardized "span_only" value):

- Spans carry rich metadata: gen_ai.system ("aws.bedrock", the legacy
  dialect -- no gen_ai.provider.name), gen_ai.request.model,
  gen_ai.operation.name ("chat"), gen_ai.usage.{input,output}_tokens,
  gen_ai.response.finish_reasons, error.type + an "exception" span event on
  failure. See test_llm_semconv.py::test_normalize_bedrock_legacy_system_dialect
  for the exact adapter mapping (no _semconv.py changes were needed).
- Message CONTENT (request messages / response choice) is NEVER present on
  the span, under any content-env value. The extension
  (extensions/bedrock.py BedrockExtension.before_service_call /
  _converse_on_success) instead calls `instrumentor_context.logger.emit(...)`
  -- content lives on the OTel LOGS signal only.
- Bedrock's own content gate (extensions/bedrock_utils.py
  genai_capture_message_content()) is a case-insensitive boolean check
  against the literal string "true" -- it does NOT understand the
  ContentCapturingMode enum ("span_only" etc.) that Task 2 verified for the
  openai/anthropic instrumentors. Honeybadger's `_apply_env_gating()` only
  ever writes "span_only", so Bedrock's own content capture never turns on
  via the production gating path at all, even when include_prompts/
  include_responses are True. And even if a user manually forces the env
  var to the literal "true", the resulting log records are inert here
  because BotocoreInstrumentor().instrument() is called with only
  tracer_provider= (see honeybadger/contrib/llm/__init__.py
  _activate_instrumentors) -- no logger_provider= is passed, so
  `instrumentor_context.logger` resolves to the process-global OTel logger
  provider, which honeybadger never sets. LLMHoneybadger has no logs
  pipeline at all; only spans are bridged to Honeybadger events
  (_bridge.py's SpanExporter classes).

Conclusion: spans are usable (rich metadata), but content is categorically
unavailable through this architecture at this pin -- both because Bedrock's
own gate never opens under Honeybadger's env-gating value, AND because even
opened, the content lands on a signal (logs) the bridge never consumes.
Per the phase-2 plan's three-outcome table this is "usable spans, content
on log events only" -> metadata-only Bedrock: integrate for metadata,
assert content absent even with flags on.
"""
import json
from unittest.mock import patch

import pytest

pytest.importorskip("opentelemetry.instrumentation.botocore")
boto3 = pytest.importorskip("boto3")
botocore_stub = pytest.importorskip("botocore.stub")

from botocore.stub import Stubber

from honeybadger import honeybadger
from honeybadger.contrib.llm import LLMHoneybadger, CONTENT_ENV_VAR
from honeybadger.tests.contrib.llm_recording import RecordingProcessor

CONVERSE_RESPONSE = {
    "output": {"message": {"role": "assistant", "content": [{"text": "hello"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 9, "outputTokens": 3, "totalTokens": 12},
    "metrics": {"latencyMs": 5},
}

MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"


@pytest.fixture
def llm(monkeypatch):
    # PRODUCTION gating path: env var unset; _apply_env_gating must set
    # "span_only" (it does, regardless of provider) -- and this test suite
    # exists specifically to prove that value never unlocks Bedrock content.
    monkeypatch.delenv(CONTENT_ENV_VAR, raising=False)
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True, "include_responses": True}},
    )
    instance = LLMHoneybadger(instruments=["bedrock"])
    instance.init()
    recorder = RecordingProcessor()
    instance._provider.add_span_processor(recorder)
    instance.recorder = recorder
    yield instance
    instance.tearDown()


def bedrock_client():
    return boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def flush(instance):
    instance._provider.force_flush()


def test_converse_end_to_end(llm):
    client = bedrock_client()
    stubber = Stubber(client)
    stubber.add_response("converse", CONVERSE_RESPONSE)
    with stubber, patch.object(honeybadger, "event") as mock_event:
        client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        )
        flush(llm)
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"
    assert data["provider"] == "aws.bedrock"
    assert data["model"] == MODEL_ID
    assert data["input_tokens"] == 9
    assert data["output_tokens"] == 3
    assert data["finish_reason"] == "end_turn"
    assert data["duration"] >= 0

    raw = llm.recorder.spans[-1].attributes
    assert raw["gen_ai.system"] == "aws.bedrock"
    assert "gen_ai.provider.name" not in raw  # legacy dialect only

    # Content: even with include_prompts/include_responses True and the
    # production env-gating path having set OTEL_INSTRUMENTATION_GENAI_
    # CAPTURE_MESSAGE_CONTENT=span_only, Bedrock never puts content on the
    # span at all -- confirm both the raw span and the emitted event agree.
    assert "gen_ai.input.messages" not in raw
    assert "gen_ai.output.messages" not in raw
    assert "prompts" not in data
    assert "response" not in data


def test_converse_error_end_to_end(llm):
    client = bedrock_client()
    stubber = Stubber(client)
    stubber.add_client_error(
        "converse",
        service_error_code="ThrottlingException",
        service_message="Too many requests",
        http_status_code=429,
    )
    with stubber, patch.object(honeybadger, "event") as mock_event:
        with pytest.raises(Exception):
            client.converse(
                modelId=MODEL_ID,
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )
        flush(llm)
    data = mock_event.call_args[0][1]
    assert data.get("error") == "ThrottlingException"
    raw = llm.recorder.spans[-1].attributes
    assert raw["error.type"] == "ThrottlingException"
    # No usage/response attributes on a failed call.
    assert "gen_ai.usage.input_tokens" not in raw
    assert "prompts" not in data
    assert "response" not in data


def test_invoke_model_end_to_end(llm):
    # Body-based API, Anthropic-on-Bedrock dialect. Also demonstrates the
    # frozen-schema rule (Task 4): gen_ai.request.max_tokens lands on the
    # raw span but _semconv._SCALAR_FIELDS has no mapping for it, so it must
    # not reach the event.
    import io

    response_body = json.dumps(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "hello from invoke_model"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 9, "output_tokens": 3},
        }
    ).encode()
    import botocore.response

    client = bedrock_client()
    stubber = Stubber(client)
    stubber.add_response(
        "invoke_model",
        {
            "body": botocore.response.StreamingBody(
                io.BytesIO(response_body), len(response_body)
            ),
            "contentType": "application/json",
        },
    )
    with stubber, patch.object(honeybadger, "event") as mock_event:
        client.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            ),
        )
        flush(llm)
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"
    assert data["provider"] == "aws.bedrock"
    assert data["model"] == MODEL_ID
    assert data["input_tokens"] == 9
    assert data["output_tokens"] == 3
    assert data["finish_reason"] == "end_turn"

    raw = llm.recorder.spans[-1].attributes
    assert raw["gen_ai.request.max_tokens"] == 64  # present on the raw span...
    assert "max_tokens" not in data  # ...but frozen schema keeps it off the event
    assert "prompts" not in data
    assert "response" not in data


def test_content_capture_never_reaches_event_even_when_bedrocks_own_gate_is_forced_on(
    monkeypatch,
):
    # Sharpest test of the architectural gap: force the ONE literal value
    # ("true") that actually opens Bedrock's own content gate
    # (genai_capture_message_content() in bedrock_utils.py) -- unlike
    # "span_only", which the production _apply_env_gating() path sets and
    # which Bedrock's own gate does not recognize at all. Content still
    # never reaches the event, because BotocoreInstrumentor is instrumented
    # with tracer_provider= only (no logger_provider=), so its log emits go
    # to the process-global (never-configured-by-honeybadger) logger
    # provider -- the bridge only consumes spans, never logs.
    monkeypatch.setenv(CONTENT_ENV_VAR, "true")
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True, "include_responses": True}},
    )
    instance = LLMHoneybadger(instruments=["bedrock"])
    instance.init()
    recorder = RecordingProcessor()
    instance._provider.add_span_processor(recorder)
    try:
        client = bedrock_client()
        stubber = Stubber(client)
        stubber.add_response("converse", CONVERSE_RESPONSE)
        with stubber, patch.object(honeybadger, "event") as mock_event:
            client.converse(
                modelId=MODEL_ID,
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )
            instance._provider.force_flush()
        data = mock_event.call_args[0][1]
        raw = recorder.spans[-1].attributes
        assert "gen_ai.input.messages" not in raw
        assert "gen_ai.output.messages" not in raw
        assert "prompts" not in data
        assert "response" not in data
    finally:
        instance.tearDown()
