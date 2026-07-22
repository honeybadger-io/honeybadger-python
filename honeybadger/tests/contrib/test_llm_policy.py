import json

from honeybadger.contrib.llm._policy import (
    apply_content_policy,
    enforce_event_budget,
    TRUNCATION_MARKER,
    OMITTED_PART,
    apply_opaque_content_policy,
)


def test_policy_drops_non_text_parts():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "content": "look at this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    ]
    result = apply_content_policy(messages, [], 100)
    assert result[0]["content"] == ["look at this", OMITTED_PART]


def test_policy_redacts_keys_inside_messages():
    messages = [{"role": "user", "content": "hi", "password": "hunter2"}]
    result = apply_content_policy(messages, ["password"], 100)
    assert result[0]["password"] == "[FILTERED]"


def test_policy_truncates_each_content_string():
    messages = [{"role": "user", "content": "x" * 50}]
    result = apply_content_policy(messages, [], 10)
    assert result[0]["content"] == "x" * 10 + TRUNCATION_MARKER


def test_policy_truncation_is_unicode_safe():
    messages = [{"role": "user", "content": "é" * 50}]
    result = apply_content_policy(messages, [], 10)
    assert result[0]["content"].startswith("é" * 10)


def test_policy_does_not_mutate_input():
    messages = [{"role": "user", "content": "x" * 50, "password": "s"}]
    apply_content_policy(messages, ["password"], 10)
    assert messages[0]["content"] == "x" * 50
    assert messages[0]["password"] == "s"


def test_policy_none_passthrough():
    assert apply_content_policy(None, [], 10) is None


def test_budget_noop_when_under():
    data = {"event_type_unused": 1, "prompts": [{"role": "user", "content": "hi"}]}
    result = enforce_event_budget(dict(data), 65536)
    assert "content_dropped" not in result


def test_budget_drops_oldest_prompts_first_keeps_system_and_response():
    prompts = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": "m%d" % i + "x" * 200} for i in range(10)
    ]
    data = {
        "prompts": prompts,
        "response": [{"role": "assistant", "content": "answer"}],
    }
    result = enforce_event_budget(data, 900)
    assert result["content_dropped"] is True
    assert result["prompts"][0]["role"] == "system"  # kept
    assert result["response"][0]["content"] == "answer"  # kept
    assert len(result["prompts"]) < 11
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 900


def test_budget_metadata_only_event_is_untouched():
    data = {"provider": "openai", "model": "gpt-4o"}
    assert enforce_event_budget(dict(data), 10) == data  # nothing droppable


def test_budget_drops_response_when_no_prompts_to_drop():
    data = {
        "provider": "openai",
        "response": [{"role": "assistant", "content": "y" * 200}],
    }
    result = enforce_event_budget(dict(data), 100)
    assert result["content_dropped"] is True
    assert "response" not in result
    assert result["provider"] == "openai"


def test_budget_drops_preserved_system_prompt_when_still_over():
    prompts = [
        {"role": "system", "content": "s" * 500},
        {"role": "user", "content": "hi"},
    ]
    data = {"prompts": prompts}
    result = enforce_event_budget(dict(data), 100)
    assert result["content_dropped"] is True
    assert "prompts" not in result


def test_budget_drops_prompts_then_response_when_both_needed():
    data = {
        "provider": "openai",
        "prompts": [{"role": "user", "content": "x" * 300}],
        "response": [{"role": "assistant", "content": "y" * 300}],
    }
    result = enforce_event_budget(dict(data), 50)
    assert result["content_dropped"] is True
    assert "prompts" not in result
    assert "response" not in result
    assert result["provider"] == "openai"  # metadata backstop, untouched


# --- apply_opaque_content_policy ---


def test_opaque_plain_string_truncated():
    result = apply_opaque_content_policy("x" * 100, [], 10)
    assert result == "x" * 10 + TRUNCATION_MARKER


def test_opaque_short_plain_string_unchanged():
    assert apply_opaque_content_policy("sunny in Paris", [], 8192) == "sunny in Paris"


def test_opaque_json_string_decoded_and_redacted():
    raw = json.dumps({"city": "Paris", "password": "hunter2"})
    result = apply_opaque_content_policy(raw, ["password"], 8192)
    assert result["city"] == "Paris"
    assert result["password"] == "[FILTERED]"


def test_opaque_nested_structures_truncate_every_string_leaf():
    value = {"a": ["y" * 50, {"b": "z" * 50}], "c": 7}
    result = apply_opaque_content_policy(value, [], 10)
    assert result["a"][0] == "y" * 10 + TRUNCATION_MARKER
    assert result["a"][1]["b"] == "z" * 10 + TRUNCATION_MARKER
    assert result["c"] == 7


def test_opaque_filter_keys_at_depth():
    value = {"outer": [{"api_key": "secret", "ok": "fine"}]}
    result = apply_opaque_content_policy(value, ["api_key"], 8192)
    assert result["outer"][0]["api_key"] == "[FILTERED]"
    assert result["outer"][0]["ok"] == "fine"


def test_opaque_unicode_preserved():
    assert apply_opaque_content_policy("héllo wörld", [], 8192) == "héllo wörld"


def test_opaque_never_mutates_input():
    value = {"a": ["y" * 50]}
    apply_opaque_content_policy(value, [], 10)
    assert value == {"a": ["y" * 50]}


def test_opaque_non_json_string_kept_as_string():
    # A plain string that merely LOOKS like it might be JSON must not raise.
    assert apply_opaque_content_policy("{not json", [], 8192) == "{not json"


def test_opaque_tuple_treated_as_list():
    # OTel stores sequence-valued attrs as tuples. A top-level tuple must
    # not bypass redaction/truncation the way a bare isinstance(value, list)
    # check would miss it.
    value = ({"api_key": "secret", "note": "x" * 50}, "y" * 50)
    result = apply_opaque_content_policy(value, ["api_key"], 10)
    assert isinstance(result, list)
    assert result[0]["api_key"] == "[FILTERED]"
    assert result[0]["note"] == "x" * 10 + TRUNCATION_MARKER
    assert result[1] == "y" * 10 + TRUNCATION_MARKER


# --- budget drop order extension ---


def test_budget_drops_opaque_content_before_prompts():
    data = {
        "arguments": "a" * 30000,
        "result": "r" * 30000,
        "input": "i" * 30000,
        "output": "o" * 30000,
        "prompts": [{"role": "user", "content": "keep me"}],
        "response": [{"role": "assistant", "content": "keep me too"}],
    }
    result = enforce_event_budget(data, 1000)
    assert "arguments" not in result
    assert "result" not in result
    assert "input" not in result
    assert "output" not in result
    assert result["prompts"]  # prompts survived: opaque content dropped first
    assert result["response"]
    assert result["content_dropped"] is True


def test_budget_drop_order_stops_as_soon_as_under():
    data = {"arguments": "a" * 30000, "result": "small", "output": "small"}
    result = enforce_event_budget(data, 1000)
    assert "arguments" not in result
    assert result["result"] == "small"  # dropping arguments was enough
    assert result["output"] == "small"
    assert result["content_dropped"] is True
