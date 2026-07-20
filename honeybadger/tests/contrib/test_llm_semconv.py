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
