import json

from honeybadger.contrib.llm._semconv import normalize, NormalizedLLMSpan
from honeybadger.tests.contrib.llm_helpers import FakeSpan, FakeEvent, FakeStatus


def chat_attributes(**overrides):
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "server.address": "api.openai.com",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.response.model": "gpt-4o-2024-08-06",
        "gen_ai.usage.input_tokens": 12,
        "gen_ai.usage.output_tokens": 34,
        "gen_ai.response.id": "chatcmpl-abc",
        "gen_ai.response.finish_reasons": ("stop",),
        "gen_ai.request.temperature": 0.5,
    }
    attrs.update(overrides)
    return attrs


def test_normalize_chat_metadata():
    span = FakeSpan(attributes=chat_attributes())
    n = normalize(span)
    assert isinstance(n, NormalizedLLMSpan)
    assert n.event_type == "llm.chat"
    assert n.data["provider"] == "openai"
    assert n.data["host"] == "api.openai.com"
    assert n.data["model"] == "gpt-4o"
    assert n.data["response_model"] == "gpt-4o-2024-08-06"
    assert n.data["input_tokens"] == 12
    assert n.data["output_tokens"] == 34
    assert n.data["provider_response_id"] == "chatcmpl-abc"
    assert n.data["finish_reason"] == "stop"
    assert n.data["temperature"] == 0.5
    assert n.data["duration"] == 1234  # (2_234_000_000 - 1_000_000_000) ns -> ms
    assert n.data["trace_id"] == format(0x1F, "032x")


def test_normalize_omits_absent_fields():
    span = FakeSpan(attributes={"gen_ai.operation.name": "chat"})
    n = normalize(span)
    assert "input_tokens" not in n.data
    assert "model" not in n.data
    assert "error" not in n.data
    assert n.prompts is None
    assert n.response is None


def test_normalize_decodes_json_messages_and_system_instructions():
    attrs = chat_attributes(
        **{
            "gen_ai.input.messages": json.dumps(
                [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]
            ),
            "gen_ai.output.messages": json.dumps(
                [{"role": "assistant", "parts": [{"type": "text", "content": "hello"}]}]
            ),
            "gen_ai.system_instructions": json.dumps(
                [{"type": "text", "content": "be brief"}]
            ),
        }
    )
    n = normalize(FakeSpan(attributes=attrs))
    # system instructions fold in as a leading role: system message
    assert n.prompts[0] == {"role": "system", "content": "be brief"}
    assert n.prompts[1]["role"] == "user"
    assert n.response[0]["role"] == "assistant"


def test_normalize_tolerates_malformed_message_json():
    attrs = chat_attributes(**{"gen_ai.input.messages": "{not json"})
    n = normalize(FakeSpan(attributes=attrs))
    assert n.prompts is None  # degrade, don't raise
    assert n.data["model"] == "gpt-4o"  # metadata still present


def test_normalize_cache_token_split():
    attrs = chat_attributes(
        **{
            "gen_ai.usage.cache_read.input_tokens": 7,
            "gen_ai.usage.cache_creation.input_tokens": 3,
        }
    )
    n = normalize(FakeSpan(attributes=attrs))
    assert n.data["cache_read_tokens"] == 7
    assert n.data["cache_creation_tokens"] == 3


def test_normalize_error_extraction_order():
    # 1) error.type attribute wins
    n = normalize(
        FakeSpan(attributes=chat_attributes(**{"error.type": "RateLimitError"}))
    )
    assert n.data["error"] == "RateLimitError"
    # 2) exception event
    span = FakeSpan(
        attributes=chat_attributes(),
        events=[FakeEvent("exception", {"exception.type": "APITimeoutError"})],
    )
    assert normalize(span).data["error"] == "APITimeoutError"
    # 3) status description
    span = FakeSpan(
        attributes=chat_attributes(),
        status=FakeStatus(description="boom", is_ok=False),
    )
    assert normalize(span).data["error"] == "boom"


def test_normalize_finish_reasons_string_not_indexed():
    # A bare string is itself iterable; guard against yielding "s" (its
    # first character) instead of the whole reason.
    attrs = chat_attributes(**{"gen_ai.response.finish_reasons": "stop"})
    n = normalize(FakeSpan(attributes=attrs))
    assert n.data["finish_reason"] == "stop"


def test_normalize_bedrock_legacy_system_dialect():
    # OBSERVED (opentelemetry-instrumentation-botocore==0.64b0,
    # extensions/bedrock.py BedrockExtension.extract_attributes(), against
    # both converse() and invoke_model()): Bedrock spans carry the legacy
    # `gen_ai.system` attribute (value "aws.bedrock") -- there is no
    # `gen_ai.provider.name` on these spans, unlike the OpenAI/Anthropic
    # instrumentors. `gen_ai.operation.name` IS present ("chat") for both
    # the Converse and InvokeModel APIs, so classification into "llm.chat"
    # works out of the box. This locks in that the adapter's existing
    # legacy-fallback SCALAR_FIELDS mapping (`gen_ai.system` -> "provider")
    # already handles the Bedrock dialect without any _semconv.py change.
    # Content (gen_ai.input.messages / gen_ai.output.messages) is never
    # present on Bedrock spans at this pin -- the instrumentor emits
    # message content on the logs signal only (see test_llm_bedrock.py).
    attrs = {
        "rpc.system": "aws-api",
        "rpc.service": "Bedrock Runtime",
        "rpc.method": "Converse",
        "gen_ai.system": "aws.bedrock",
        "gen_ai.request.model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "gen_ai.operation.name": "chat",
        "gen_ai.usage.input_tokens": 9,
        "gen_ai.usage.output_tokens": 3,
        "gen_ai.response.finish_reasons": ["end_turn"],
    }
    n = normalize(FakeSpan(attributes=attrs))
    assert n.event_type == "llm.chat"
    assert n.data["provider"] == "aws.bedrock"
    assert n.data["model"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert n.data["input_tokens"] == 9
    assert n.data["output_tokens"] == 3
    assert n.data["finish_reason"] == "end_turn"  # raw pass-through, no "stop" mapping
    assert n.prompts is None  # no gen_ai.input.messages on this dialect
    assert n.response is None  # no gen_ai.output.messages either


def test_normalize_classifies_embeddings_and_unknown():
    emb = FakeSpan(
        attributes={
            "gen_ai.operation.name": "embeddings",
            "gen_ai.request.model": "text-embedding-3-small",
            "gen_ai.usage.input_tokens": 5,
        }
    )
    assert normalize(emb).event_type == "llm.embedding"

    unknown = FakeSpan(attributes={"gen_ai.operation.name": "moderation"})
    assert normalize(unknown).event_type == "llm.call"

    not_llm = FakeSpan(attributes={"http.method": "GET"})
    assert normalize(not_llm) is None


def test_span_id_and_parent_span_id_extracted():
    from honeybadger.tests.contrib.llm_helpers import FakeSpanContext

    span = FakeSpan(
        attributes={"gen_ai.operation.name": "chat"},
        span_id=0xABC,
        parent=FakeSpanContext(span_id=0xDEF),
    )
    result = normalize(span)
    assert result.data["span_id"] == format(0xABC, "016x")
    assert result.data["parent_span_id"] == format(0xDEF, "016x")


def test_parent_span_id_omitted_for_root_spans():
    span = FakeSpan(attributes={"gen_ai.operation.name": "chat"}, parent=None)
    result = normalize(span)
    assert "parent_span_id" not in result.data


def test_ts_set_from_span_start_time():
    import datetime

    span = FakeSpan(
        attributes={"gen_ai.operation.name": "chat"},
        start_time=1_700_000_000_000_000_000,  # ns
    )
    result = normalize(span)
    assert result.data["ts"] == datetime.datetime.fromtimestamp(
        1_700_000_000, datetime.timezone.utc
    )


def test_ts_omitted_when_no_start_time():
    span = FakeSpan(attributes={"gen_ai.operation.name": "chat"}, start_time=None)
    result = normalize(span)
    assert "ts" not in result.data


def test_conversation_id_mapped_when_present():
    span = FakeSpan(
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.conversation.id": "thread-42",
        }
    )
    result = normalize(span)
    assert result.data["conversation_id"] == "thread-42"


def test_span_ids_survive_broken_context():
    class NoContextSpan(FakeSpan):
        def get_span_context(self):
            raise RuntimeError("boom")

    result = normalize(NoContextSpan(attributes={"gen_ai.operation.name": "chat"}))
    assert "span_id" not in result.data
    assert "trace_id" not in result.data
