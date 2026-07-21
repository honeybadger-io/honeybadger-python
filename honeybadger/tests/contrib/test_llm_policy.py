import json

from honeybadger.contrib.llm._policy import (
    apply_content_policy,
    enforce_event_budget,
    TRUNCATION_MARKER,
    OMITTED_PART,
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
