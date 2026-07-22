"""End-to-end: OpenAI Agents SDK instrumentor + genai-openai provider
instrumentor over a mocked transport (chat-completions model). Skipped
without the phase-3 deps."""

import asyncio
from unittest.mock import patch

import pytest

pytest.importorskip("opentelemetry.instrumentation.genai.openai_agents")
pytest.importorskip("agents")
httpx = pytest.importorskip("httpx")

from agents import Agent, Runner, RunConfig, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI, APIConnectionError

from honeybadger import honeybadger
from honeybadger.contrib.llm import LLMHoneybadger, CONTENT_ENV_VAR

TOOL_CALL_RESPONSE = {
    "id": "chatcmpl-agents-tool",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "gpt-4o-2024-08-06",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_xyz789",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Paris"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 22, "completion_tokens": 9, "total_tokens": 31},
}

FINAL_RESPONSE = {
    "id": "chatcmpl-agents-final",
    "object": "chat.completion",
    "created": 1700000001,
    "model": "gpt-4o-2024-08-06",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Sunny in Paris."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45},
}


@function_tool
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return f"sunny in {city}"


def make_agent(name="WeatherAgent"):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        body = TOOL_CALL_RESPONSE if calls["n"] == 1 else FINAL_RESPONSE
        return httpx.Response(200, json=body)

    client = AsyncOpenAI(
        api_key="sk-test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return Agent(
        name=name,
        instructions="You answer weather questions.",
        model=OpenAIChatCompletionsModel(model="gpt-4o", openai_client=client),
        tools=[get_weather],
    )


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv(CONTENT_ENV_VAR, "span_only")
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True, "include_responses": True}},
    )
    instance = LLMHoneybadger(
        instruments=["openai_agents", "openai"],
        instrument_options={"openai_agents": {"disable_openai_trace_export": True}},
    )
    instance.init()
    yield instance
    instance.tearDown()


def collect(llm_instance, run):
    with patch.object(honeybadger, "event") as mock_event:
        run()
        llm_instance._provider.force_flush()
    return [(c.args[0], c.args[1]) for c in mock_event.call_args_list]


def by_type(events, event_type):
    return [d for (t, d) in events if t == event_type]


def test_run_sync_emits_reconstructable_tree(llm):
    events = collect(
        llm,
        lambda: Runner.run_sync(
            make_agent(),
            "weather in Paris?",
            run_config=RunConfig(workflow_name="weather-workflow"),
        ),
    )
    workflows = by_type(events, "llm.workflow")
    agents_ = by_type(events, "llm.agent")
    tools_ = by_type(events, "llm.tool_call")
    chats = by_type(events, "llm.chat")

    assert len(workflows) == 1
    assert workflows[0]["workflow_name"] == "weather-workflow"
    assert "parent_span_id" not in workflows[0]
    assert len(agents_) == 1
    assert agents_[0]["agent_name"] == "WeatherAgent"
    assert len(tools_) == 1
    assert tools_[0]["tool_name"] == "get_weather"
    assert tools_[0]["result"] == "sunny in Paris"  # include_responses on
    assert len(chats) == 2  # provider spans only; no dedup needed here

    # tree: workflow > agent > (chat, tool)
    assert agents_[0]["parent_span_id"] == workflows[0]["span_id"]
    assert tools_[0]["parent_span_id"] == agents_[0]["span_id"]
    for chat in chats:
        assert chat["parent_span_id"] == agents_[0]["span_id"]
    assert len({d["trace_id"] for (_t, d) in events}) == 1

    # framework attribution
    assert workflows[0]["framework"] == "openai_agents"


def test_async_run(llm):
    events = collect(
        llm, lambda: asyncio.run(Runner.run(make_agent(), "weather in Paris?"))
    )
    types = [t for (t, _d) in events]
    assert types.count("llm.workflow") == 1
    assert types.count("llm.agent") == 1
    assert types.count("llm.tool_call") == 1
    assert types.count("llm.chat") == 2


def test_concurrent_runs_keep_traces_separate(llm):
    async def two_runs():
        await asyncio.gather(
            Runner.run(make_agent("AgentA"), "weather in Paris?"),
            Runner.run(make_agent("AgentB"), "weather in Berlin?"),
        )

    events = collect(llm, lambda: asyncio.run(two_runs()))
    workflows = by_type(events, "llm.workflow")
    assert len(workflows) == 2
    trace_a, trace_b = workflows[0]["trace_id"], workflows[1]["trace_id"]
    assert trace_a != trace_b
    # every event belongs to exactly one run; parent links never cross traces.
    # OBSERVED (openai-agents 0.x + genai-openai-agents, this pin): every
    # emitted parent_span_id does resolve to a span_id that produced an event
    # in the SAME trace, so the strict per-brief assertion holds as written --
    # no relaxation to the "no OTHER trace" fallback was needed. Keeping the
    # strict form since it's a stronger guarantee; recorded here per the
    # brief's request either way.
    spans_by_trace = {}
    for _t, d in events:
        spans_by_trace.setdefault(d["trace_id"], set()).add(d["span_id"])
    for _t, d in events:
        if "parent_span_id" in d:
            assert d["parent_span_id"] in spans_by_trace[d["trace_id"]]


def test_aborted_run_emits_completed_spans(llm):
    """Adverse: the run dies mid-way (2nd model call fails at transport
    level). Whatever spans completed before the abort must still emit --
    never silent loss of the whole run."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=TOOL_CALL_RESPONSE)
        raise httpx.ConnectError("network down")

    client = AsyncOpenAI(
        api_key="sk-test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    agent = Agent(
        name="WeatherAgent",
        instructions="You answer weather questions.",
        model=OpenAIChatCompletionsModel(model="gpt-4o", openai_client=client),
        tools=[get_weather],
    )

    def run():
        # OBSERVED (openai SDK + openai-agents 0.x, this pin): the raised
        # httpx.ConnectError is caught inside AsyncOpenAI's request retry
        # loop (openai._base_client.BaseClient.request) and re-wrapped as
        # openai.APIConnectionError before propagating out of
        # Runner.run_sync -- it is not the raw httpx exception, nor an
        # agents.exceptions.* wrapper. The brief's `pytest.raises(Exception)`
        # is intentionally loose to cover this; asserting the concrete
        # observed type here to record it.
        with pytest.raises(Exception) as exc_info:
            Runner.run_sync(agent, "weather in Paris?")
        assert isinstance(exc_info.value, APIConnectionError)

    events = collect(llm, run)
    types = [t for (t, _d) in events]
    # first model call + tool completed before the abort; adapt the exact
    # error-span expectations to observed behavior and record them.
    assert types.count("llm.chat") >= 1
    assert types.count("llm.tool_call") == 1


def test_reinit_after_teardown_still_emits(llm):
    """Regression: openai_agents' uninstrument does not clear the util-genai
    TelemetryHandler singleton; without our lifecycle fix, the second init
    binds framework spans to the dead provider and this test emits nothing."""
    llm.tearDown()
    second = LLMHoneybadger(
        instruments=["openai_agents", "openai"],
        instrument_options={"openai_agents": {"disable_openai_trace_export": True}},
    )
    second.init()
    try:
        events = collect(
            second, lambda: Runner.run_sync(make_agent(), "weather in Paris?")
        )
        assert by_type(events, "llm.workflow")
    finally:
        second.tearDown()
