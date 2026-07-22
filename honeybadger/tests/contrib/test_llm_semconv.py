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


# --- framework span classification + normalizers ---


def test_classification_table():
    cases = {
        "chat": "llm.chat",
        "embeddings": "llm.embedding",
        "invoke_workflow": "llm.workflow",
        "invoke_agent": "llm.agent",
        "execute_tool": "llm.tool_call",
        "something_new": "llm.call",  # fallthrough unchanged
    }
    for operation, expected in cases.items():
        span = FakeSpan(attributes={"gen_ai.operation.name": operation})
        assert normalize(span).event_type == expected, operation


def test_workflow_name_from_attribute():
    span = FakeSpan(
        attributes={
            "gen_ai.operation.name": "invoke_workflow",
            "gen_ai.workflow.name": "weather-workflow",
        },
        name="invoke_workflow weather-workflow",
    )
    result = normalize(span)
    assert result.event_type == "llm.workflow"
    assert result.data["workflow_name"] == "weather-workflow"


def test_workflow_name_parsed_from_span_name_fallback():
    # LangChain at 1.0b0 puts the name only in the span name.
    span = FakeSpan(
        attributes={"gen_ai.operation.name": "invoke_workflow"},
        name="invoke_workflow LangGraph",
    )
    assert normalize(span).data["workflow_name"] == "LangGraph"


def test_workflow_name_omitted_when_bare_span_name():
    span = FakeSpan(
        attributes={"gen_ai.operation.name": "invoke_workflow"},
        name="invoke_workflow",
    )
    assert "workflow_name" not in normalize(span).data


def test_workflow_content_carries_raw_input_output():
    raw_in = json.dumps([{"role": "user", "parts": [{"type": "text", "content": "q"}]}])
    raw_out = json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": "a"}]}]
    )
    span = FakeSpan(
        attributes={
            "gen_ai.operation.name": "invoke_workflow",
            "gen_ai.input.messages": raw_in,
            "gen_ai.output.messages": raw_out,
        },
        name="invoke_workflow G",
    )
    result = normalize(span)
    assert result.content == {"input": raw_in, "output": raw_out}
    assert result.prompts is None  # framework events never use prompts/response
    assert result.response is None


def test_agent_fields():
    span = FakeSpan(
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "WeatherAgent",
            "gen_ai.agent.id": "agent-1",
            "gen_ai.agent.description": "Answers weather questions",
            "gen_ai.conversation.id": "thread-42",
        },
        name="invoke_agent WeatherAgent",
    )
    result = normalize(span)
    assert result.event_type == "llm.agent"
    assert result.data["agent_name"] == "WeatherAgent"
    assert result.data["agent_id"] == "agent-1"
    assert result.data["description"] == "Answers weather questions"
    assert result.data["conversation_id"] == "thread-42"
    assert result.content == {}


def test_tool_fields_and_content():
    span = FakeSpan(
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "get_weather",
            "gen_ai.tool.call.id": "call_abc123",
            "gen_ai.tool.type": "function",
            "gen_ai.tool.call.arguments": '{"city":"Paris"}',
            "gen_ai.tool.call.result": "sunny in Paris",
        },
        name="execute_tool get_weather",
    )
    result = normalize(span)
    assert result.event_type == "llm.tool_call"
    assert result.data["tool_name"] == "get_weather"
    assert result.data["tool_call_id"] == "call_abc123"
    assert result.data["tool_type"] == "function"
    assert result.content == {
        "arguments": '{"city":"Paris"}',
        "result": "sunny in Paris",
    }


def test_framework_events_skip_provider_scalar_fields():
    # An agent span carrying model/usage attrs (util-genai supports them)
    # must NOT map them: the llm.agent schema has no such fields.
    span = FakeSpan(
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "A",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 5,
        }
    )
    data = normalize(span).data
    assert "model" not in data
    assert "input_tokens" not in data


def test_framework_events_still_get_duration_error_and_tree_fields():
    from honeybadger.tests.contrib.llm_helpers import FakeSpanContext

    span = FakeSpan(
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "t",
            "error.type": "ValueError",
        },
        parent=FakeSpanContext(span_id=0xB),
    )
    data = normalize(span).data
    assert data["duration"] == 1234
    assert data["error"] == "ValueError"
    assert data["trace_id"] == format(0x1F, "032x")
    assert data["parent_span_id"] == format(0xB, "016x")
    assert "ts" in data


def test_chat_events_have_empty_content_dict():
    span = FakeSpan(attributes={"gen_ai.operation.name": "chat"})
    assert normalize(span).content == {}
