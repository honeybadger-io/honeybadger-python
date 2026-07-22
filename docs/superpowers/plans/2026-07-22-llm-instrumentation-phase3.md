# LLM Instrumentation Phase 3 (Frameworks) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Instrument LangChain/LangGraph and OpenAI Agents SDK apps so a single agent run appears in Insights as a reconstructable tree: `llm.workflow`, `llm.agent`, `llm.tool_call`, plus the existing `llm.chat` events joined via `trace_id`/`span_id`/`parent_span_id`.

**Architecture:** Extend the existing span→event bridge (`honeybadger/contrib/llm/`) with operation-name classification (`invoke_workflow`/`invoke_agent`/`execute_tool`), a response-identity dedup LRU for the double chat spans LangChain produces, an opaque-content policy for tool/workflow content, run-tree fields on all span events, and a `sampling_key` mechanism in core so whole runs sample atomically. OTLP mode stays a raw-span pipe (content attrs scrubbed only).

**Tech Stack:** `opentelemetry-instrumentation-genai-langchain==1.0b0`, `opentelemetry-instrumentation-genai-openai-agents==1.0b0`, `opentelemetry-util-genai==1.0b0`, existing phase 1+2 contrib.

**Spec:** `docs/superpowers/specs/2026-07-20-llm-instrumentation-phase3-design.md` (stacked on phase 2, branch `llm-py-phase3-frameworks`, PR target `llm-py-phase2-anthropic-bedrock`).

## Global Constraints

- Pins: both new instrumentors `>=1.0b0,<1.1`, env markers `python_version >= "3.10"` (setup.py `[llm]` extra and dev-requirements.txt).
- Event type names (frozen): `llm.workflow`, `llm.agent`, `llm.tool_call`. Field names exactly as in spec §"Event schemas".
- Every field is best-effort: omit when the source is absent — never emit `None`.
- OTLP mode gets **no routing and no dedup** — only `_CONTENT_ATTRS` additions with opaque scrubbing.
- Dedup ordering invariant: the `exclude_models` check runs BEFORE the dedup-LRU insert, so an excluded event never suppresses its twin.
- Never disturb another telemetry consumer: Agents SDK native trace exporter stays active by default; the util-genai `TelemetryHandler` singleton is only cleared at tearDown if our init created it.
- Run tests with: `.venv/bin/python -m pytest` from the repo root.

## Empirical findings (probes run 2026-07-22, both instrumentors at 1.0b0 — resolve spec checkpoints; cite these in the maintainer doc, Task 11)

1. Operation names: `invoke_workflow`, `invoke_agent`, `execute_tool` (spec checkpoint #1).
2. No framework-identifying span attribute exists. `gen_ai.workflow.name` is set **only** by the openai-agents instrumentor (non-semconv extra); LangChain workflow spans carry only `gen_ai.operation.name` (+ content attrs) — name only in the span name `invoke_workflow {name}` (checkpoints #2, #6).
3. LangChain emits its own chat span as the **parent** of the provider (genai-openai) chat span; both carry identical `gen_ai.response.id` and identical usage/finish/model fields; only the provider span has `server.address`. The provider span (child) ends first, so first-seen-wins keeps the richer one. The kept event's `parent_span_id` points at the suppressed twin (dangling pointer — join via `trace_id`; document) (checkpoint #3).
4. Framework spans all carry `gen_ai.*` attributes (at minimum `gen_ai.operation.name`) — OTLP GenAI gate passes them (checkpoint #4).
5. `span_only` content env is honored by both (shared util-genai `should_capture_content_on_spans()`); tool `gen_ai.tool.call.arguments`/`result` and workflow `gen_ai.input.messages`/`gen_ai.output.messages` appear only when set (checkpoint #5).
6. Attribute names (checkpoint #6): tool → `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`, `gen_ai.tool.description`, `gen_ai.tool.call.arguments` (JSON string or plain string), `gen_ai.tool.call.result` (plain string observed); agent → `gen_ai.agent.name`, `gen_ai.agent.id`, `gen_ai.agent.description`, `gen_ai.conversation.id`; workflow input/output → standard `gen_ai.input.messages`/`gen_ai.output.messages` (message-shaped JSON strings, LangChain only).
7. Detection dists: `importlib.metadata.version("openai-agents")` and `("langchain-core")` both resolve; there is **no** dist named `agents` (checkpoint #7).
8. **Singleton gotcha:** both framework instrumentors get their `TelemetryHandler` from the `get_telemetry_handler()` module singleton; the first creator binds the tracer provider, later callers' `tracer_provider=` args are ignored. `LangChainInstrumentor.uninstrument()` clears the singleton; `OpenAIAgentsInstrumentor.uninstrument()` does NOT — without cleanup, a tearDown/re-init cycle with only `openai_agents` reuses a handler bound to the dead provider and silently drops all framework spans.
9. LangChain behavior at this pin: a vanilla `create_agent`/LangGraph run emits **no** agent span (workflow + chat + tool only). Supplying `metadata={"agent_name": ...}` on `invoke()` makes the **root** an `invoke_agent` span (with `gen_ai.agent.id`/`.description`/`gen_ai.conversation.id` from `agent_id`/`agent_description`/`thread_id|session_id|conversation_id` metadata) and **suppresses the workflow span** (agent classification precedes workflow classification for the root chain).
10. OpenAI Agents SDK at this pin: tool spans carry **no** `gen_ai.tool.call.id` and **no** `gen_ai.tool.call.arguments` (the instrumentor reads `span_data.input` at span start, before the SDK fills it); `gen_ai.tool.call.result` is present. Chat spans come only from the provider instrumentor — no duplicates. Workflow span carries `gen_ai.workflow.name`; agent span carries only `gen_ai.agent.name`.

---

### Task 1: Core sampling key (`_hb.sampling_key`)

**Files:**
- Modify: `honeybadger/core.py` (`_should_sample_event`, ~line 185)
- Test: `honeybadger/tests/test_core.py`

**Interfaces:**
- Consumes: existing `_hb` payload metadata convention (stripped before send).
- Produces: `payload["_hb"]["sampling_key"]` (str) takes highest precedence as the sampling hash key; falls back to `request_id`, then a random UUID. Task 5's bridge sets this key.

- [ ] **Step 1: Write the failing tests** (append to `honeybadger/tests/test_core.py`)

```python
def test_event_sampling_key_from_hb_metadata():
    """Events sharing _hb.sampling_key sample identically (all-or-nothing)."""
    mock_events_worker = MagicMock()
    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True, events_sample_rate=50)

    # Same sampling_key, different request_ids: identical decision for all.
    for i in range(10):
        hb.event(
            "llm.chat",
            {
                "request_id": "req-%d" % i,
                "_hb": {"sampling_key": "trace-abc"},
            },
        )
    assert mock_events_worker.push.call_count in (0, 10)

    # And the decision matches hashing the sampling_key itself.
    import hashlib

    expected = (
        int(hashlib.md5(b"trace-abc").hexdigest(), 16) % 100
    ) < 50
    assert (mock_events_worker.push.call_count == 10) == expected


def test_event_sampling_key_falls_back_to_request_id():
    """Without _hb.sampling_key the existing request_id behavior is unchanged."""
    mock_events_worker = MagicMock()
    hb = Honeybadger()
    hb.events_worker = mock_events_worker
    hb.configure(api_key="aaa", force_report_data=True, events_sample_rate=50)

    import hashlib

    expected = (int(hashlib.md5(b"req-1").hexdigest(), 16) % 100) < 50
    hb.event("test.event", {"request_id": "req-1"})
    assert (mock_events_worker.push.call_count == 1) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest honeybadger/tests/test_core.py -k sampling_key -v`
Expected: FAIL — `test_event_sampling_key_from_hb_metadata` fails because `_should_sample_event` ignores `_hb["sampling_key"]` and hashes per-event `request_id`s (or random UUIDs), so the 10 events don't sample atomically / don't match the `trace-abc` hash.

- [ ] **Step 3: Implement** — in `honeybadger/core.py::_should_sample_event`, replace:

```python
        sampling_key = payload.get("request_id")
        if not sampling_key:
            sampling_key = str(uuid.uuid4())
```

with:

```python
        # _hb.sampling_key (set e.g. by the LLM bridge to the run's trace_id
        # so a whole run samples in or out atomically) takes precedence over
        # request_id; a fresh UUID (independent sampling) is the fallback.
        sampling_key = hb_metadata.get("sampling_key") or payload.get("request_id")
        if not sampling_key:
            sampling_key = str(uuid.uuid4())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest honeybadger/tests/test_core.py -v`
Expected: PASS (all, including pre-existing sampling tests).

- [ ] **Step 5: Commit**

```bash
git add honeybadger/core.py honeybadger/tests/test_core.py
git commit -m "feat(core): honor _hb.sampling_key for atomic event sampling"
```

---

### Task 2: Opaque-content policy + extended budget drop order

**Files:**
- Modify: `honeybadger/contrib/llm/_policy.py`
- Test: `honeybadger/tests/contrib/test_llm_policy.py`

**Interfaces:**
- Consumes: `honeybadger.utils.filter_structure(structure, filter_keys)` (existing).
- Produces:
  - `apply_opaque_content_policy(value, filter_keys, max_content_length) -> Any` — pure; JSON-decodes JSON-string input, key-redacts any structure, truncates every string leaf, returns a JSON-serializable value. Used by Task 5 (events) and Task 6 (OTLP scrub).
  - `enforce_event_budget` drop order becomes: `arguments` → `result` → `input` → `output` → prompts-oldest-first → response; sets `content_dropped` as today.

- [ ] **Step 1: Write the failing tests** (append to `honeybadger/tests/contrib/test_llm_policy.py`; match its existing import style — it imports from `honeybadger.contrib.llm._policy`)

```python
from honeybadger.contrib.llm._policy import (
    apply_opaque_content_policy,
    TRUNCATION_MARKER,
)


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
```

Note: `json` and `enforce_event_budget` are already imported at the top of the existing test file; add only the missing imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm_policy.py -v`
Expected: FAIL with `ImportError: cannot import name 'apply_opaque_content_policy'`.

- [ ] **Step 3: Implement** — in `honeybadger/contrib/llm/_policy.py` add:

```python
def apply_opaque_content_policy(value, filter_keys: list, max_content_length: int):
    """Policy for any-typed opaque content (tool arguments/results, workflow
    input/output). JSON-decode when the value is a JSON string, key-redact
    any structure, truncate EVERY string leaf (not just "content" keys).
    Pure -- never mutates its input. Returns a JSON-serializable value."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass  # plain string content: truncate below
    if isinstance(value, (dict, list)):
        value = filter_structure(value, filter_keys)
    return _truncate_string_leaves(value, max_content_length)


def _truncate_string_leaves(value, max_length: int):
    if isinstance(value, str):
        if len(value) > max_length:
            return value[:max_length] + TRUNCATION_MARKER
        return value
    if isinstance(value, list):
        return [_truncate_string_leaves(item, max_length) for item in value]
    if isinstance(value, dict):
        return {
            key: _truncate_string_leaves(item, max_length)
            for key, item in value.items()
        }
    return value
```

and in `enforce_event_budget`, insert immediately after `dropped_any = False`:

```python
    # Opaque framework content drops first, in fixed order (spec).
    for key in ("arguments", "result", "input", "output"):
        if _size(data) <= max_event_bytes:
            break
        if key in data:
            del data[key]
            dropped_any = True
```

Caveat: `filter_structure` may mutate — verify with `honeybadger/utils.py`; if it mutates its argument, deep-copy first (`json.loads(json.dumps(...))` is NOT acceptable — use `copy.deepcopy`). The `test_opaque_never_mutates_input` test enforces this.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm_policy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add honeybadger/contrib/llm/_policy.py honeybadger/tests/contrib/test_llm_policy.py
git commit -m "feat(llm): opaque-content policy and extended budget drop order"
```

---

### Task 3: Run-tree fields on all span events (`span_id`, `parent_span_id`, span-start `ts`, `conversation_id`)

**Files:**
- Modify: `honeybadger/contrib/llm/_semconv.py`
- Modify: `honeybadger/tests/contrib/llm_helpers.py` (FakeSpan gains `parent`)
- Test: `honeybadger/tests/contrib/test_llm_semconv.py`

**Interfaces:**
- Consumes: ReadableSpan duck-type — adds `span.parent` (a SpanContext with `.span_id`, or None) to the surface `normalize()` reads.
- Produces: every `NormalizedLLMSpan.data` gains (when sources exist): `span_id` (16-hex str), `parent_span_id` (16-hex str, omitted for roots), `ts` (`datetime` from `span.start_time` ns — `honeybadger.event()` honors a provided `ts`), `conversation_id` (from `gen_ai.conversation.id`).

- [ ] **Step 1: Extend FakeSpan** — in `honeybadger/tests/contrib/llm_helpers.py`, add a `parent=None` constructor param stored as `self.parent`, and allow overriding the context ids:

```python
class FakeSpan:
    """Duck-types the ReadableSpan surface the bridge reads."""

    def __init__(
        self,
        attributes=None,
        events=None,
        status=None,
        start_time=1_000_000_000,
        end_time=2_234_000_000,
        name="chat gpt-4o",
        parent=None,
        trace_id=0x1F,
        span_id=0x2,
    ):
        self.attributes = attributes or {}
        self.events = events or []
        self.status = status or FakeStatus()
        self.start_time = start_time  # ns
        self.end_time = end_time  # ns
        self.name = name
        self.parent = parent  # SpanContext duck-type (FakeSpanContext) or None
        self._ctx = FakeSpanContext(trace_id=trace_id, span_id=span_id)

    def get_span_context(self):
        return self._ctx
```

- [ ] **Step 2: Write the failing tests** (append to `honeybadger/tests/contrib/test_llm_semconv.py`; it imports `normalize` and `FakeSpan` already — check and reuse its helpers)

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm_semconv.py -v`
Expected: new tests FAIL (`span_id` etc. not in data); all pre-existing tests PASS.

- [ ] **Step 4: Implement** — in `honeybadger/contrib/llm/_semconv.py`:

Add `import datetime` at the top. Add `"gen_ai.conversation.id": "conversation_id"` to `_SCALAR_FIELDS`. Add helpers:

```python
def _span_id(span) -> Optional[str]:
    try:
        return format(span.get_span_context().span_id, "016x")
    except Exception:
        return None


def _parent_span_id(span) -> Optional[str]:
    parent = getattr(span, "parent", None)
    if parent is None:
        return None
    try:
        return format(parent.span_id, "016x")
    except Exception:
        return None


def _start_ts(span) -> Optional["datetime.datetime"]:
    start = getattr(span, "start_time", None)
    if start is None:
        return None
    return datetime.datetime.fromtimestamp(
        start / 1_000_000_000, datetime.timezone.utc
    )
```

and in `normalize()`, next to the existing `trace_id` block:

```python
    span_id = _span_id(span)
    if span_id:
        data["span_id"] = span_id
    parent_span_id = _parent_span_id(span)
    if parent_span_id:
        data["parent_span_id"] = parent_span_id
    ts = _start_ts(span)
    if ts is not None:
        data["ts"] = ts
```

- [ ] **Step 5: Run the full LLM unit suite**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm_semconv.py honeybadger/tests/contrib/test_llm.py -v`
Expected: PASS (existing `test_llm.py` bridge tests must not break — they use FakeSpan whose new `parent` defaults to None).

- [ ] **Step 6: Commit**

```bash
git add honeybadger/contrib/llm/_semconv.py honeybadger/tests/contrib/llm_helpers.py honeybadger/tests/contrib/test_llm_semconv.py
git commit -m "feat(llm): run-tree fields (span_id/parent_span_id/start-ts/conversation_id) on all span events"
```

---

### Task 4: Classification + workflow/agent/tool normalizers

**Files:**
- Modify: `honeybadger/contrib/llm/_semconv.py`
- Test: `honeybadger/tests/contrib/test_llm_semconv.py`

**Interfaces:**
- Produces:
  - `_OPERATION_EVENT_TYPES` gains `"invoke_workflow": "llm.workflow"`, `"invoke_agent": "llm.agent"`, `"execute_tool": "llm.tool_call"`.
  - `FRAMEWORK_EVENT_TYPES = frozenset({"llm.workflow", "llm.agent", "llm.tool_call"})` (module constant; Task 5's bridge imports it).
  - `NormalizedLLMSpan` gains field `content: Dict[str, Any]` (default empty dict) carrying **raw attribute values** keyed `"arguments"`/`"result"` (tool) or `"input"`/`"output"` (workflow). The bridge gates and policies them (Task 5).
  - Framework events do NOT populate `prompts`/`response` and do not apply `_SCALAR_FIELDS` (their schemas per spec: no provider/model/token fields).
  - Workflow: `workflow_name` from `gen_ai.workflow.name` attr, else parsed from span name `invoke_workflow {name}`.
  - Agent: `agent_name`, `agent_id`, `description` (from `gen_ai.agent.name/.id/.description`).
  - Tool: `tool_name`, `tool_call_id`, `tool_type` (from `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`).

- [ ] **Step 1: Write the failing tests** (append to `test_llm_semconv.py`)

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm_semconv.py -v`
Expected: new tests FAIL (classification falls through to `llm.call`, no `content` field).

- [ ] **Step 3: Implement** — in `honeybadger/contrib/llm/_semconv.py`:

Extend the table and add constants:

```python
_OPERATION_EVENT_TYPES = {
    "chat": "llm.chat",
    "embeddings": "llm.embedding",
    "embedding": "llm.embedding",
    "invoke_workflow": "llm.workflow",
    "invoke_agent": "llm.agent",
    "execute_tool": "llm.tool_call",
}

FRAMEWORK_EVENT_TYPES = frozenset({"llm.workflow", "llm.agent", "llm.tool_call"})

# framework-event attribute -> event field (direct copies, per event type)
_AGENT_FIELDS = {
    "gen_ai.agent.name": "agent_name",
    "gen_ai.agent.id": "agent_id",
    "gen_ai.agent.description": "description",
    "gen_ai.conversation.id": "conversation_id",
}

_TOOL_FIELDS = {
    "gen_ai.tool.name": "tool_name",
    "gen_ai.tool.call.id": "tool_call_id",
    "gen_ai.tool.type": "tool_type",
    "gen_ai.conversation.id": "conversation_id",
}
```

Restructure `normalize()`: keep the gen_ai gate and operation lookup, then branch. The existing scalar/finish-reason/prompts/response extraction becomes the non-framework path; the shared tail (duration, trace_id, span_id, parent_span_id, ts, error) runs for every event. Shape:

```python
def normalize(span) -> Optional[NormalizedLLMSpan]:
    attributes = dict(span.attributes or {})
    if not any(key.startswith("gen_ai.") for key in attributes):
        return None

    operation: Any = attributes.get("gen_ai.operation.name")
    event_type = _OPERATION_EVENT_TYPES.get(operation, "llm.call")  # type: ignore[arg-type]

    data: Dict[str, Any] = {}
    content: Dict[str, Any] = {}
    prompts: Optional[List[dict]] = None
    response: Optional[List[dict]] = None

    if event_type == "llm.workflow":
        workflow_name = attributes.get("gen_ai.workflow.name") or _parse_span_name(
            span, "invoke_workflow"
        )
        if workflow_name:
            data["workflow_name"] = workflow_name
        if "gen_ai.conversation.id" in attributes:
            data["conversation_id"] = attributes["gen_ai.conversation.id"]
        for attr, key in (
            ("gen_ai.input.messages", "input"),
            ("gen_ai.output.messages", "output"),
        ):
            if attr in attributes:
                content[key] = attributes[attr]
    elif event_type == "llm.agent":
        for attr, field_name in _AGENT_FIELDS.items():
            if attr in attributes:
                data[field_name] = attributes[attr]
    elif event_type == "llm.tool_call":
        for attr, field_name in _TOOL_FIELDS.items():
            if attr in attributes:
                data[field_name] = attributes[attr]
        for attr, key in (
            ("gen_ai.tool.call.arguments", "arguments"),
            ("gen_ai.tool.call.result", "result"),
        ):
            if attr in attributes:
                content[key] = attributes[attr]
    else:
        # chat / embedding / llm.call: unchanged phase-1/2 extraction
        for attr, field_name in _SCALAR_FIELDS.items():
            if attr in attributes and field_name not in data:
                data[field_name] = attributes[attr]
        finish_reasons = attributes.get("gen_ai.response.finish_reasons")
        if finish_reasons:
            data["finish_reason"] = (
                finish_reasons
                if isinstance(finish_reasons, str)
                else list(finish_reasons)[0]
            )
        prompts = _decode_messages(attributes.get("gen_ai.input.messages"))
        system = _decode_system_instructions(
            attributes.get("gen_ai.system_instructions")
        )
        if system:
            prompts = [{"role": "system", "content": system}] + (prompts or [])
        response = _decode_messages(attributes.get("gen_ai.output.messages"))

    # shared tail: duration/trace/span ids/ts/error (all events)
    ...

    return NormalizedLLMSpan(
        event_type=event_type,
        data=data,
        prompts=prompts,
        response=response,
        content=content,
    )


def _parse_span_name(span, operation) -> Optional[str]:
    name = getattr(span, "name", None) or ""
    prefix = operation + " "
    if name.startswith(prefix):
        return name[len(prefix):] or None
    return None
```

`NormalizedLLMSpan` gains the field (use `field(default_factory=dict)` from dataclasses):

```python
@dataclass
class NormalizedLLMSpan:
    event_type: str
    data: Dict[str, Any]
    prompts: Optional[List[dict]]
    response: Optional[List[dict]]
    content: Dict[str, Any] = field(default_factory=dict)
```

Also add `"gen_ai.conversation.id": "conversation_id"` to `_SCALAR_FIELDS` if not already done in Task 3 (chat path).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm_semconv.py honeybadger/tests/contrib/test_llm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add honeybadger/contrib/llm/_semconv.py honeybadger/tests/contrib/test_llm_semconv.py
git commit -m "feat(llm): classify framework spans into llm.workflow/agent/tool_call events"
```

---

### Task 5: Bridge events mode — dedup LRU, content gating, framework attribution, sampling key

**Files:**
- Modify: `honeybadger/contrib/llm/_bridge.py`
- Test: `honeybadger/tests/contrib/test_llm.py`

**Interfaces:**
- Consumes: `FRAMEWORK_EVENT_TYPES`, `NormalizedLLMSpan.content` (Task 4); `apply_opaque_content_policy` (Task 2).
- Produces:
  - `ResponseDedup(maxsize=512)` class in `_bridge.py` with `check_and_add(key) -> bool` (True = already seen → drop) and `clear()`.
  - `_export_one` reads `getattr(owner, "_dedup", None)` (dedup checker) and `getattr(owner, "active_frameworks", ())` (tuple of active framework registry keys). Task 7 wires both onto `LLMHoneybadger`.
  - Emit order inside `_export_one`: normalize → exclusion → **dedup** → content gating (prompts/response + opaque content) → context lift → budget → `_hb.sampling_key` → `honeybadger.event`.
  - Opaque gating map (module constant): `_OPAQUE_CONTENT_FLAGS = {"arguments": "include_prompts", "input": "include_prompts", "result": "include_responses", "output": "include_responses"}`.

- [ ] **Step 1: Write the failing tests** (append to `honeybadger/tests/contrib/test_llm.py`; reuse its `owner()`, `configured()`, `chat_span()` helpers — extend `owner()`):

Replace the existing `owner()` helper with:

```python
def owner(active=True, dedup=None, frameworks=()):
    return SimpleNamespace(
        active=active, _dedup=dedup, active_frameworks=tuple(frameworks)
    )
```

New tests:

```python
# --- chat dedup (response identity LRU) ---

from honeybadger.contrib.llm._bridge import ResponseDedup


def dedup_owner(**kwargs):
    return owner(dedup=ResponseDedup(), **kwargs)


def test_dedup_drops_second_chat_span_with_same_response_id():
    configured()
    o = dedup_owner()
    span_a = chat_span(**{"gen_ai.response.id": "chatcmpl-1"})
    span_b = chat_span(**{"gen_ai.response.id": "chatcmpl-1"})
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([span_a, span_b], o)
    assert mock_event.call_count == 1


def test_dedup_scopes_by_trace_id():
    configured()
    o = dedup_owner()
    span_a = chat_span(**{"gen_ai.response.id": "chatcmpl-1"})
    span_b = chat_span(**{"gen_ai.response.id": "chatcmpl-1"})
    span_b._ctx.trace_id = 0x99  # different trace: not a duplicate
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([span_a, span_b], o)
    assert mock_event.call_count == 2


def test_chat_spans_without_response_id_never_suppressed():
    configured()
    o = dedup_owner()
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span(), chat_span()], o)
    assert mock_event.call_count == 2


def test_excluded_event_does_not_suppress_its_twin():
    # exclusion check runs BEFORE the dedup insert: if the first span is
    # excluded, the second (same response id, different model) still emits.
    configured(exclude_models=["gpt-4o"])
    o = dedup_owner()
    excluded = chat_span(**{"gen_ai.response.id": "chatcmpl-1"})
    kept = chat_span(
        **{"gen_ai.response.id": "chatcmpl-1", "gen_ai.request.model": "gpt-4o-mini"}
    )
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([excluded, kept], o)
    assert mock_event.call_count == 1
    assert mock_event.call_args[0][1]["model"] == "gpt-4o-mini"


def test_dedup_lru_bound_and_eviction():
    lru = ResponseDedup(maxsize=2)
    assert lru.check_and_add(("t", "a")) is False
    assert lru.check_and_add(("t", "b")) is False
    assert lru.check_and_add(("t", "c")) is False  # evicts ("t", "a")
    assert lru.check_and_add(("t", "a")) is False  # evicted -> not seen
    assert lru.check_and_add(("t", "c")) is True


def test_dedup_clear():
    lru = ResponseDedup()
    lru.check_and_add(("t", "a"))
    lru.clear()
    assert lru.check_and_add(("t", "a")) is False


def test_export_works_without_dedup_attribute():
    # owner without _dedup (phase-1/2 stubs, borrowed cases): no dedup, no crash.
    configured()
    with patch.object(honeybadger, "event") as mock_event:
        export_spans(
            [chat_span(**{"gen_ai.response.id": "x"})],
            SimpleNamespace(active=True),
        )
    assert mock_event.call_count == 1


# --- opaque content gating on framework events ---


def tool_span(**attr_overrides):
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "get_weather",
        "gen_ai.tool.call.id": "call_1",
        "gen_ai.tool.call.arguments": json.dumps({"city": "Paris", "password": "x"}),
        "gen_ai.tool.call.result": "sunny",
    }
    attrs.update(attr_overrides)
    return FakeSpan(attributes=attrs, name="execute_tool get_weather")


def test_tool_content_gated_independently():
    configured(include_prompts=True, include_responses=False)
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([tool_span()], owner())
    data = mock_event.call_args[0][1]
    assert data["arguments"]["city"] == "Paris"
    assert "result" not in data


def test_tool_content_redacted_via_opaque_policy():
    configured(include_prompts=True, include_responses=True)
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([tool_span()], owner())
    data = mock_event.call_args[0][1]
    assert data["arguments"]["password"] == "[FILTERED]"
    assert data["result"] == "sunny"


def test_tool_content_dropped_by_default():
    configured()  # both flags default False
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([tool_span()], owner())
    data = mock_event.call_args[0][1]
    assert "arguments" not in data
    assert "result" not in data
    assert data["tool_name"] == "get_weather"


def test_workflow_input_output_gated():
    configured(include_prompts=False, include_responses=True)
    span = FakeSpan(
        attributes={
            "gen_ai.operation.name": "invoke_workflow",
            "gen_ai.input.messages": json.dumps([{"role": "user"}]),
            "gen_ai.output.messages": json.dumps([{"role": "assistant"}]),
        },
        name="invoke_workflow G",
    )
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([span], owner())
    data = mock_event.call_args[0][1]
    assert "input" not in data
    assert data["output"] == [{"role": "assistant"}]


# --- framework attribution ---


def test_framework_set_when_single_framework_active():
    configured()
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([tool_span()], owner(frameworks=("langchain",)))
    assert mock_event.call_args[0][1]["framework"] == "langchain"


def test_framework_omitted_when_ambiguous_or_absent():
    configured()
    for frameworks in ((), ("langchain", "openai_agents")):
        with patch.object(honeybadger, "event") as mock_event:
            export_spans([tool_span()], owner(frameworks=frameworks))
        assert "framework" not in mock_event.call_args[0][1]


def test_framework_never_set_on_chat_events():
    configured()
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner(frameworks=("langchain",)))
    assert "framework" not in mock_event.call_args[0][1]


# --- sampling key ---


def test_sampling_key_set_to_trace_id():
    configured()
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner())
    data = mock_event.call_args[0][1]
    assert data["_hb"] == {"sampling_key": data["trace_id"]}


def test_sampling_key_omitted_without_trace_id():
    configured()

    class NoContextSpan(FakeSpan):
        def get_span_context(self):
            raise RuntimeError("no context")

    with patch.object(honeybadger, "event") as mock_event:
        export_spans(
            [NoContextSpan(attributes={"gen_ai.operation.name": "chat"})], owner()
        )
    assert "_hb" not in mock_event.call_args[0][1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm.py -v`
Expected: new tests FAIL (`ImportError: ResponseDedup`, etc.). Pre-existing tests PASS (the reworked `owner()` keeps `active` semantics).

- [ ] **Step 3: Implement** — in `honeybadger/contrib/llm/_bridge.py`:

```python
from collections import OrderedDict

from ._semconv import normalize, _flatten_parts, FRAMEWORK_EVENT_TYPES
from ._policy import (
    apply_content_policy,
    apply_opaque_content_policy,
    enforce_event_budget,
)

# opaque content key -> gating flag (arguments/input are prompt-side,
# result/output are response-side)
_OPAQUE_CONTENT_FLAGS = {
    "arguments": "include_prompts",
    "input": "include_prompts",
    "result": "include_responses",
    "output": "include_responses",
}

_DEDUPED_EVENT_TYPES = ("llm.chat", "llm.embedding")


class ResponseDedup:
    """Bounded LRU of (trace_id, provider_response_id) keys already emitted.
    Best-effort suppression of the double chat spans LangChain produces
    (its own chat span wraps the provider instrumentor's). NOT thread-safe
    beyond the GIL; only the export thread touches it."""

    def __init__(self, maxsize=512):
        self.maxsize = maxsize
        self._seen = OrderedDict()

    def check_and_add(self, key):
        """True when key was already emitted (caller drops the event);
        records the key otherwise."""
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = True
        if len(self._seen) > self.maxsize:
            self._seen.popitem(last=False)
        return False

    def clear(self):
        self._seen.clear()
```

Rework `_export_one`:

```python
def _export_one(span, owner):
    if not getattr(owner, "active", False):
        return
    config = honeybadger.config
    llm_config = config.insights_config.llm
    if not config.insights_enabled or llm_config.disabled:
        return

    normalized = normalize(span)
    if normalized is None:
        return

    data = normalized.data
    if _excluded(data.get("model"), llm_config.exclude_models):
        return

    # Response-identity dedup: LangChain emits its own chat span around the
    # provider instrumentor's for the same model call. Runs AFTER exclusion
    # (an excluded event must never suppress its twin), BEFORE emit.
    dedup = getattr(owner, "_dedup", None)
    if (
        dedup is not None
        and normalized.event_type in _DEDUPED_EVENT_TYPES
        and data.get("provider_response_id")
        and data.get("trace_id")
    ):
        if dedup.check_and_add((data["trace_id"], data["provider_response_id"])):
            return

    if llm_config.include_prompts and normalized.prompts is not None:
        data["prompts"] = apply_content_policy(
            normalized.prompts, config.params_filters, llm_config.max_content_length
        )
    if llm_config.include_responses and normalized.response is not None:
        data["response"] = apply_content_policy(
            normalized.response, config.params_filters, llm_config.max_content_length
        )
    for key, raw in normalized.content.items():
        if getattr(llm_config, _OPAQUE_CONTENT_FLAGS[key]):
            data[key] = apply_opaque_content_policy(
                raw, config.params_filters, llm_config.max_content_length
            )

    if normalized.event_type in FRAMEWORK_EVENT_TYPES:
        frameworks = getattr(owner, "active_frameworks", ()) or ()
        if len(frameworks) == 1:
            # Attribution is derivable only when exactly one framework
            # instrumentor is active; never guess when ambiguous.
            data["framework"] = frameworks[0]

    # Lift honeybadger.context.* BEFORE budgeting so lifted context counts
    # against max_event_bytes too -- otherwise a large context value could
    # push the serialized event back over budget after enforcement.
    for key, value in (span.attributes or {}).items():
        if key.startswith(CONTEXT_ATTR_PREFIX):
            data.setdefault(key[len(CONTEXT_ATTR_PREFIX) :], value)

    data = enforce_event_budget(data, llm_config.max_event_bytes)

    # After budgeting: _hb is internal metadata (stripped by event()), it
    # must not count against or be dropped by the content budget.
    if data.get("trace_id"):
        data["_hb"] = {"sampling_key": data["trace_id"]}

    honeybadger.event(normalized.event_type, data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm.py honeybadger/tests/contrib/test_llm_semconv.py honeybadger/tests/contrib/test_llm_policy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add honeybadger/contrib/llm/_bridge.py honeybadger/tests/contrib/test_llm.py
git commit -m "feat(llm): response-identity dedup, opaque content gating, framework attribution, sampling key"
```

---

### Task 6: OTLP scrub of tool content attributes

**Files:**
- Modify: `honeybadger/contrib/llm/_bridge.py` (`_CONTENT_ATTRS`, `scrub_attributes`, new `_scrub_opaque_attr`)
- Test: `honeybadger/tests/contrib/test_llm.py`

**Interfaces:**
- Consumes: `apply_opaque_content_policy` (Task 2).
- Produces: `_CONTENT_ATTRS` gains `"gen_ai.tool.call.arguments": "include_prompts"` and `"gen_ai.tool.call.result": "include_responses"`; those two route to the opaque scrubber (`_scrub_opaque_attr`) instead of the message-shaped `_scrub_content_attr`. Workflow input/output reuse the existing `gen_ai.input.messages`/`gen_ai.output.messages` entries (already message-shaped — no change). OTLP mode keeps NO routing/dedup.

- [ ] **Step 1: Write the failing tests** (append to `test_llm.py`, near the existing `scrub_*` tests — reuse their `llm_config`/filters setup style; read the neighboring tests first and copy their fixture usage exactly):

```python
def test_scrub_drops_tool_content_attrs_by_default():
    configured()
    llm_config = honeybadger.config.insights_config.llm
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "get_weather",
        "gen_ai.tool.call.arguments": '{"city":"Paris"}',
        "gen_ai.tool.call.result": "sunny",
    }
    result = _bridge.scrub_attributes(attrs, llm_config, [])
    assert "gen_ai.tool.call.arguments" not in result
    assert "gen_ai.tool.call.result" not in result
    assert result["gen_ai.tool.name"] == "get_weather"


def test_scrub_keeps_and_redacts_tool_arguments_when_opted_in():
    configured(include_prompts=True, include_responses=True)
    llm_config = honeybadger.config.insights_config.llm
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.call.arguments": json.dumps(
            {"city": "Paris", "password": "hunter2"}
        ),
        "gen_ai.tool.call.result": "sunny in Paris",
    }
    result = _bridge.scrub_attributes(attrs, llm_config, ["password"])
    decoded = json.loads(result["gen_ai.tool.call.arguments"])
    assert decoded["city"] == "Paris"
    assert decoded["password"] == "[FILTERED]"
    # plain-string result passes through the opaque policy as a string
    assert result["gen_ai.tool.call.result"] == "sunny in Paris"


def test_scrub_truncates_plain_string_tool_result():
    configured(include_prompts=True, include_responses=True, max_content_length=5)
    llm_config = honeybadger.config.insights_config.llm
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.call.result": "sunny in Paris",
    }
    result = _bridge.scrub_attributes(attrs, llm_config, [])
    assert result["gen_ai.tool.call.result"].startswith("sunny")
    assert "TRUNCATED" in result["gen_ai.tool.call.result"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm.py -k scrub -v`
Expected: new tests FAIL (tool attrs pass through unscrubbed today).

- [ ] **Step 3: Implement** — in `_bridge.py`:

```python
_CONTENT_ATTRS = {
    "gen_ai.input.messages": "include_prompts",
    "gen_ai.system_instructions": "include_prompts",
    "gen_ai.tool.definitions": "include_prompts",
    "gen_ai.output.messages": "include_responses",
    "gen_ai.tool.call.arguments": "include_prompts",
    "gen_ai.tool.call.result": "include_responses",
}

# Attrs that hold any-typed opaque content (plain string or JSON), not
# message lists -- scrubbed with the opaque policy, not the message policy.
_OPAQUE_ATTRS = frozenset(
    {"gen_ai.tool.call.arguments", "gen_ai.tool.call.result"}
)
```

In `scrub_attributes`, route:

```python
        if key in _OPAQUE_ATTRS:
            result[key] = _scrub_opaque_attr(
                value, params_filters, llm_config.max_content_length
            )
        else:
            result[key] = _scrub_content_attr(
                value, params_filters, llm_config.max_content_length
            )
```

New function:

```python
def _scrub_opaque_attr(raw, params_filters, max_content_length):
    import json as _json

    from ._policy import apply_opaque_content_policy

    policied = apply_opaque_content_policy(raw, params_filters, max_content_length)
    if isinstance(policied, str):
        return policied  # keep plain strings as plain attribute values
    return _json.dumps(policied, ensure_ascii=False, default=repr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add honeybadger/contrib/llm/_bridge.py honeybadger/tests/contrib/test_llm.py
git commit -m "feat(llm): otlp-mode scrub for tool call arguments/result attributes"
```

---

### Task 7: Registry + detection + `instrument_options` + singleton lifecycle + dedup wiring

**Files:**
- Modify: `honeybadger/contrib/llm/__init__.py`
- Test: `honeybadger/tests/contrib/test_llm.py`

**Interfaces:**
- Consumes: `_bridge.ResponseDedup` (Task 5).
- Produces:
  - Registry entries `"langchain"` and `"openai_agents"` as 4-tuples `(sdk_module, instrumentor_module, class_name, dist_name)`; existing 3-tuples unchanged. Helper `_registry_entry(key) -> (sdk_module, module_name, class_name, dist_name_or_None)`.
  - `_FRAMEWORK_KEYS = frozenset({"langchain", "openai_agents"})`.
  - `LLMHoneybadger(instruments=None, tracer_provider=None, export="events", instrument_options=None)` — `instrument_options` is a dict of per-registry-key kwarg dicts merged into `instrumentor.instrument(...)`.
  - `LLMHoneybadger.active_frameworks` property → tuple of activated framework keys.
  - `self._dedup = _bridge.ResponseDedup()` created in `init()`, `.clear()`ed in `_cleanup_wiring()` after provider flush/shutdown.
  - TelemetryHandler singleton lifecycle: if our init caused `get_telemetry_handler._default_handler` to come into existence, tearDown deletes it; if a foreign singleton pre-exists at framework activation, log a warning (framework spans may route to another provider).

- [ ] **Step 1: Write the failing tests** (append to `test_llm.py`)

```python
# --- phase 3: registry, detection, instrument_options, singleton lifecycle ---


def test_registry_contains_phase3_frameworks():
    assert llm_module._INSTRUMENTORS["langchain"] == (
        "langchain_core",
        "opentelemetry.instrumentation.genai.langchain",
        "LangChainInstrumentor",
        "langchain-core",
    )
    assert llm_module._INSTRUMENTORS["openai_agents"] == (
        "agents",
        "opentelemetry.instrumentation.genai.openai_agents",
        "OpenAIAgentsInstrumentor",
        "openai-agents",
    )


def test_frameworks_are_auto_detected_but_bedrock_still_is_not():
    instance = LLMHoneybadger()
    requested = instance._requested_instruments()
    assert "langchain" in requested
    assert "openai_agents" in requested
    assert "bedrock" not in requested


def test_dist_based_detection_skips_wrong_agents_package(monkeypatch):
    """openai_agents activation must check the openai-agents DISTRIBUTION,
    not find_spec("agents") -- any unrelated package could claim that name."""
    import importlib.metadata

    calls = []

    def fake_version(dist):
        calls.append(dist)
        raise importlib.metadata.PackageNotFoundError(dist)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    instance = LLMHoneybadger(instruments=["openai_agents"])
    activated = llm_module._activate_instrumentors(instance, provider=None)
    assert activated == []
    assert "openai-agents" in calls


def test_instrument_options_passed_through(monkeypatch):
    recorded = {}

    class FakeInstrumentor:
        is_instrumented_by_opentelemetry = False

        def instrument(self, **kwargs):
            recorded.update(kwargs)

        def uninstrument(self):
            pass

    fake_module = SimpleNamespace(FakeInstrumentor=FakeInstrumentor)
    monkeypatch.setitem(
        llm_module._INSTRUMENTORS,
        "openai_agents",
        ("agents", "fake.module", "FakeInstrumentor", "openai-agents"),
    )
    import importlib
    import importlib.metadata

    monkeypatch.setattr(importlib, "import_module", lambda name: fake_module)
    monkeypatch.setattr(importlib.metadata, "version", lambda dist: "0.18.3")

    instance = LLMHoneybadger(
        instruments=["openai_agents"],
        instrument_options={"openai_agents": {"disable_openai_trace_export": True}},
    )
    llm_module._activate_instrumentors(instance, provider="fake-provider")
    assert recorded == {
        "tracer_provider": "fake-provider",
        "disable_openai_trace_export": True,
    }


def test_genai_singleton_cleared_only_if_we_created_it():
    from opentelemetry.util.genai import handler as genai_handler

    get = genai_handler.get_telemetry_handler
    # Case 1: no pre-existing singleton; we created one -> cleared.
    if hasattr(get, "_default_handler"):
        delattr(get, "_default_handler")
    instance = LLMHoneybadger()
    instance._saw_genai_singleton = False
    instance._activated_framework = True
    get._default_handler = object()  # simulate framework instrument() creating it
    llm_module._release_genai_singleton(instance)
    assert not hasattr(get, "_default_handler")

    # Case 2: pre-existing singleton -> left alone.
    sentinel = object()
    get._default_handler = sentinel
    instance2 = LLMHoneybadger()
    instance2._saw_genai_singleton = True
    instance2._activated_framework = True
    llm_module._release_genai_singleton(instance2)
    assert get._default_handler is sentinel
    delattr(get, "_default_handler")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm.py -v`
Expected: new tests FAIL (3-tuple registry KeyError/assert mismatch, missing `_release_genai_singleton`, `instrument_options` TypeError). Note: `test_registry_contains_phase2_providers`, `test_otel_available_is_registry_driven`, and `test_activation_uses_registry_tuples` may need updating in Step 3 if they unpack 3-tuples — update them to use `_registry_entry` semantics, do not delete them.

- [ ] **Step 3: Implement** — in `honeybadger/contrib/llm/__init__.py`:

Registry additions (after the bedrock entry, keeping the existing comment):

```python
    # Frameworks (phase 3). 4th element = PyPI distribution name: detection
    # goes through importlib.metadata.version(dist) because the import name
    # alone is untrustworthy ("agents" is generic; langchain_core is the
    # real trigger for the langchain callback instrumentor).
    "langchain": (
        "langchain_core",
        "opentelemetry.instrumentation.genai.langchain",
        "LangChainInstrumentor",
        "langchain-core",
    ),
    "openai_agents": (
        "agents",
        "opentelemetry.instrumentation.genai.openai_agents",
        "OpenAIAgentsInstrumentor",
        "openai-agents",
    ),
```

Helpers + constants:

```python
_FRAMEWORK_KEYS = frozenset({"langchain", "openai_agents"})


def _registry_entry(key):
    entry = _INSTRUMENTORS[key]
    if len(entry) == 3:
        return entry + (None,)
    return entry


def _sdk_available(key):
    """Is the instrumented SDK importable/installed? dist_name entries verify
    the installed DISTRIBUTION (import names like "agents" are claimable by
    any package); 3-tuple entries keep find_spec (no behavior change)."""
    import importlib.metadata
    import importlib.util

    sdk_module, _module, _cls, dist_name = _registry_entry(key)
    if dist_name is not None:
        try:
            importlib.metadata.version(dist_name)
            return True
        except importlib.metadata.PackageNotFoundError:
            return False
    return importlib.util.find_spec(sdk_module) is not None
```

Update `_otel_available` to unpack via `_registry_entry` (it reads only `module_name`). Update `_activate_instrumentors`:

```python
def _activate_instrumentors(self, provider):
    """Instrument each requested/detected provider we can own. Returns keys."""
    import importlib

    activated = []
    for key in self._requested_instruments():
        _sdk_module, module_name, class_name, _dist = _registry_entry(key)
        if not _sdk_available(key):
            continue
        instrumentor_cls = getattr(importlib.import_module(module_name), class_name)
        instrumentor = instrumentor_cls()
        if instrumentor.is_instrumented_by_opentelemetry:
            logger.warning(
                "honeybadger llm: %s already instrumented by another consumer; skipping",
                key,
            )
            continue
        if key in _FRAMEWORK_KEYS and not self._activated_framework:
            # Framework instrumentors share the util-genai TelemetryHandler
            # singleton; record whether one pre-exists so tearDown only
            # clears what OUR init created (see _release_genai_singleton).
            self._saw_genai_singleton = _genai_singleton_exists()
            self._activated_framework = True
            if self._saw_genai_singleton:
                logger.warning(
                    "honeybadger llm: a util-genai TelemetryHandler already "
                    "exists; framework spans may be routed to another "
                    "consumer's tracer provider, not Honeybadger's"
                )
        kwargs = dict((self.instrument_options or {}).get(key) or {})
        instrumentor.instrument(tracer_provider=provider, **kwargs)
        self._instrumentors[key] = instrumentor
        activated.append(key)
    return activated
```

Singleton helpers (module level):

```python
def _genai_singleton_exists():
    try:
        from opentelemetry.util.genai.handler import get_telemetry_handler
    except ImportError:
        return False
    return getattr(get_telemetry_handler, "_default_handler", None) is not None


def _release_genai_singleton(self):
    """Clear the util-genai TelemetryHandler singleton iff our init created
    it. Framework instrumentors bind the singleton's tracer to whichever
    provider existed at FIRST creation; without this, tearDown + re-init
    with only openai_agents (whose uninstrument does NOT clear it, unlike
    langchain's) would silently pipe all framework spans to the dead
    provider."""
    if not getattr(self, "_activated_framework", False):
        return
    if getattr(self, "_saw_genai_singleton", True):
        return  # pre-existing singleton is someone else's; leave it
    try:
        from opentelemetry.util.genai.handler import get_telemetry_handler
    except ImportError:
        return
    if hasattr(get_telemetry_handler, "_default_handler"):
        delattr(get_telemetry_handler, "_default_handler")
```

Constructor and lifecycle changes:

```python
    def __init__(
        self, instruments=None, tracer_provider=None, export="events",
        instrument_options=None,
    ):
        ...existing body...
        self.instrument_options = instrument_options
        self._dedup = _bridge.ResponseDedup()
        self._saw_genai_singleton = False
        self._activated_framework = False

    @property
    def active_frameworks(self):
        return tuple(k for k in self._instrumentors if k in _FRAMEWORK_KEYS)
```

In `_cleanup_wiring()`, after `_deactivate_instrumentors(self)` add `_release_genai_singleton(self)`, and at the end (after `self._processor = None`) add:

```python
        self._dedup.clear()
        self._activated_framework = False
        self._saw_genai_singleton = False
```

(Keep `self._dedup` itself alive — the exporter closure reads `owner._dedup` and clearing is sufficient; a fresh `init()` starts with an empty LRU.)

- [ ] **Step 4: Fix any pre-existing registry-shape tests** (`test_registry_contains_phase2_providers`, `test_otel_available_is_registry_driven`, `test_activation_uses_registry_tuples`) to tolerate the mixed 3/4-tuple registry via `_registry_entry`.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest honeybadger/tests/ -v 2>&1 | tail -20`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add honeybadger/contrib/llm/__init__.py honeybadger/tests/contrib/test_llm.py
git commit -m "feat(llm): register langchain/openai_agents with dist detection, instrument_options, singleton lifecycle"
```

---

### Task 8: Packaging

**Files:**
- Modify: `setup.py` (extras_require `llm`)
- Modify: `dev-requirements.txt`

**Interfaces:** none (metadata only).

- [ ] **Step 1: setup.py** — append to the `"llm"` extra list (after the botocore line):

```python
            'opentelemetry-instrumentation-genai-langchain>=1.0b0,<1.1; python_version >= "3.10"',
            'opentelemetry-instrumentation-genai-openai-agents>=1.0b0,<1.1; python_version >= "3.10"',
```

- [ ] **Step 2: dev-requirements.txt** — append under the LLM block:

```
opentelemetry-instrumentation-genai-langchain>=1.0b0,<1.1; python_version >= "3.10"
opentelemetry-instrumentation-genai-openai-agents>=1.0b0,<1.1; python_version >= "3.10"
langchain; python_version >= "3.10"
langgraph; python_version >= "3.10"
langchain-openai; python_version >= "3.10"
openai-agents; python_version >= "3.10"
```

- [ ] **Step 3: Verify resolver health in the dev venv**

Run: `.venv/bin/pip install -r dev-requirements.txt -q && .venv/bin/pip check`
Expected: `No broken requirements found.`

- [ ] **Step 4: Commit**

```bash
git add setup.py dev-requirements.txt
git commit -m "feat(llm): package langchain/openai-agents framework instrumentors in [llm] extra"
```

---

### Task 9: Integration — LangChain/LangGraph

**Files:**
- Create: `honeybadger/tests/contrib/test_llm_langchain.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8; `RecordingProcessor` from `llm_recording.py`; mocked-transport pattern from `test_llm_integration.py`.
- Produces: the observed-behavior evidence rows for the maintainer doc (Task 11).

Key mechanics (verified by probe):
- `create_agent` (from `langchain.agents`) + `@tool` + `ChatOpenAI(http_client=httpx.Client(transport=MockTransport(...)))`. First mocked response returns a `tool_calls` message, second returns a final answer (copy the `TOOL_CALL_RESPONSE`/`FINAL_RESPONSE` bodies below).
- Messages must be `HumanMessage` objects — tuple messages crash the 1.0b0 callback handler (`AttributeError` in `on_chain_start`, workflow span never closes).
- `instruments=["langchain", "openai"]` so the dedup assertion runs with both instrumentors active.
- Events asserted via `patch.object(honeybadger, "event")`; raw spans via a `RecordingProcessor` added to the instance's provider **before** `init()` is not possible (provider is built in init) — instead add it right after `init()`: `llm._provider.add_span_processor(recorder)`.

- [ ] **Step 1: Write the test module**

```python
"""End-to-end: LangChain/LangGraph framework instrumentor + genai-openai
provider instrumentor against a mocked OpenAI transport. Asserts raw spans
AND emitted events per the phase-3 spec. Skipped without the phase-3 deps."""

import asyncio
import json
from unittest.mock import patch

import pytest

pytest.importorskip("opentelemetry.instrumentation.genai.langchain")
pytest.importorskip("langchain_openai")
pytest.importorskip("langgraph")
httpx = pytest.importorskip("httpx")

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from honeybadger import honeybadger
from honeybadger.contrib.llm import LLMHoneybadger, CONTENT_ENV_VAR
from honeybadger.tests.contrib.llm_recording import RecordingProcessor

TOOL_CALL_RESPONSE = {
    "id": "chatcmpl-tool1",
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
                        "id": "call_abc123",
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
    "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
}

FINAL_RESPONSE = {
    "id": "chatcmpl-final1",
    "object": "chat.completion",
    "created": 1700000001,
    "model": "gpt-4o-2024-08-06",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "It is sunny in Paris."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 35, "completion_tokens": 6, "total_tokens": 41},
}


@tool
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return f"sunny in {city}"


@tool
def broken_tool(city: str) -> str:
    """Always fails."""
    raise ValueError("tool exploded")


def two_step_handler():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        body = TOOL_CALL_RESPONSE if calls["n"] == 1 else FINAL_RESPONSE
        return httpx.Response(200, json=body)

    return handler


def sync_llm(handler):
    return ChatOpenAI(
        model="gpt-4o",
        api_key="sk-test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def async_llm(handler):
    return ChatOpenAI(
        model="gpt-4o",
        api_key="sk-test",
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv(CONTENT_ENV_VAR, "span_only")
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True, "include_responses": True}},
    )
    instance = LLMHoneybadger(instruments=["langchain", "openai"])
    instance.init()
    instance.recorder = RecordingProcessor()
    instance._provider.add_span_processor(instance.recorder)
    yield instance
    instance.tearDown()


def run_graph(events_out, llm_instance, chat_model, message="weather in Paris?",
              tools=(get_weather,), config=None):
    graph = create_agent(chat_model, list(tools))
    with patch.object(honeybadger, "event") as mock_event:
        graph.invoke({"messages": [HumanMessage(message)]}, config=config or {})
        llm_instance._provider.force_flush()
    events_out.extend((c.args[0], c.args[1]) for c in mock_event.call_args_list)


def by_type(events, event_type):
    return [d for (t, d) in events if t == event_type]


def test_langgraph_run_emits_reconstructable_tree(llm):
    events = []
    run_graph(events, llm, sync_llm(two_step_handler()))

    workflows = by_type(events, "llm.workflow")
    tools_ = by_type(events, "llm.tool_call")
    chats = by_type(events, "llm.chat")

    # one workflow, >=1 tool call, exactly one chat event per model call
    assert len(workflows) == 1
    workflow = workflows[0]
    assert workflow["workflow_name"]  # "LangGraph" at this pin
    assert "parent_span_id" not in workflow  # root
    assert len(tools_) == 1
    assert tools_[0]["tool_name"] == "get_weather"
    assert tools_[0]["tool_call_id"] == "call_abc123"
    assert len(chats) == 2  # 2 model calls -> 2 events (dedup collapsed 4 spans)
    response_ids = {c.get("provider_response_id") for c in chats}
    assert response_ids == {"chatcmpl-tool1", "chatcmpl-final1"}

    # all events share the run's trace and carry sampling keys + ts
    trace_ids = {d["trace_id"] for (_t, d) in events}
    assert len(trace_ids) == 1
    for _t, d in events:
        assert d["_hb"] == {"sampling_key": d["trace_id"]}
        assert "ts" in d and "span_id" in d

    # parentage: tool hangs off the workflow (as observed at this pin)
    assert tools_[0]["parent_span_id"] == workflow["span_id"]

    # framework attribution: only langchain is the active framework
    assert workflow["framework"] == "langchain"
    assert tools_[0]["framework"] == "langchain"
    assert all("framework" not in c for c in chats)

    # opt-in content present
    assert tools_[0]["arguments"] == {"city": "Paris"}
    assert tools_[0]["result"] == "sunny in Paris"
    assert workflow["input"]
    assert workflow["output"]

    # raw spans: exactly 6 (workflow + 2x2 chat + tool) with 2 duplicate pairs
    chat_spans = [
        s for s in llm.recorder.spans
        if (s.attributes or {}).get("gen_ai.operation.name") == "chat"
    ]
    assert len(chat_spans) == 4  # dedup happened in the exporter, not the SDK


def test_async_ainvoke_emits_same_tree(llm):
    graph = create_agent(async_llm(two_step_handler()), [get_weather])
    with patch.object(honeybadger, "event") as mock_event:
        asyncio.run(graph.ainvoke({"messages": [HumanMessage("weather in Paris?")]}))
        llm._provider.force_flush()
    types = [c.args[0] for c in mock_event.call_args_list]
    assert types.count("llm.workflow") == 1
    assert types.count("llm.tool_call") == 1
    assert types.count("llm.chat") == 2


def test_agent_metadata_promotes_root_to_agent_event(llm):
    events = []
    run_graph(
        events, llm, sync_llm(two_step_handler()),
        config={
            "metadata": {
                "agent_name": "WeatherAgent",
                "agent_id": "agent-1",
                "agent_description": "Answers weather questions",
                "thread_id": "thread-42",
            }
        },
    )
    agents = by_type(events, "llm.agent")
    assert len(agents) == 1
    agent = agents[0]
    assert agent["agent_name"] == "WeatherAgent"
    assert agent["agent_id"] == "agent-1"
    assert agent["description"] == "Answers weather questions"
    assert agent["conversation_id"] == "thread-42"
    # observed at 1.0b0: agent classification replaces the workflow root
    assert by_type(events, "llm.workflow") == []
    # chat + tool events hang off the agent
    tools_ = by_type(events, "llm.tool_call")
    assert tools_[0]["parent_span_id"] == agent["span_id"]


def test_tool_error_recorded_and_run_still_emits(llm):
    handler_calls = {"n": 0}

    def handler(request):
        handler_calls["n"] += 1
        if handler_calls["n"] == 1:
            body = json.loads(json.dumps(TOOL_CALL_RESPONSE))
            body["choices"][0]["message"]["tool_calls"][0]["function"][
                "name"
            ] = "broken_tool"
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=FINAL_RESPONSE)

    events = []
    run_graph(events, llm, sync_llm(handler), tools=(broken_tool,))
    tools_ = by_type(events, "llm.tool_call")
    assert len(tools_) == 1
    assert "error" in tools_[0]
    # the run kept going and still produced its workflow event
    assert len(by_type(events, "llm.workflow")) == 1
```

Note for the implementer: `create_agent`'s ReAct loop retries or surfaces tool errors depending on version — if `broken_tool`'s error is swallowed and converted to a ToolMessage (LangGraph default), the tool span still records the error via `on_tool_error` only when the tool node re-raises. If the observed span has no `error`, adapt the test to assert observed behavior (e.g. build a one-node `StateGraph` invoking the tool directly) — the requirement is: an error inside a tool produces an `llm.tool_call` event with `error`, and the workflow event still emits. Record what you observe for the maintainer doc.

- [ ] **Step 2: Run**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm_langchain.py -v`
Expected: PASS (iterate on observed-behavior details; keep spec invariants — dedup count, tree fields, gating — non-negotiable).

- [ ] **Step 3: Run the whole suite**

Run: `.venv/bin/python -m pytest honeybadger/tests/ 2>&1 | tail -5`
Expected: PASS, no regressions.

- [ ] **Step 4: Commit**

```bash
git add honeybadger/tests/contrib/test_llm_langchain.py
git commit -m "test(llm): langchain/langgraph end-to-end integration (tree, dedup, gating)"
```

---

### Task 10: Integration — OpenAI Agents SDK

**Files:**
- Create: `honeybadger/tests/contrib/test_llm_agents.py`

**Interfaces:**
- Consumes: Tasks 1–8; `instrument_options` pass-through (Task 7).
- Produces: evidence rows for the maintainer doc (Task 11).

Key mechanics (verified by probe):
- `Agent(model=OpenAIChatCompletionsModel(model="gpt-4o", openai_client=AsyncOpenAI(http_client=httpx.AsyncClient(transport=MockTransport(...)))))` avoids the Responses API.
- Always instrument with `instrument_options={"openai_agents": {"disable_openai_trace_export": True}}` in tests — keeps the SDK's native exporter from doing network I/O AND exercises the new escape hatch.
- At this pin, Agents-SDK tool spans have NO `gen_ai.tool.call.id`/`arguments`; assert `tool_name`/`result` instead.
- `Runner.run_sync` cannot run inside a running loop; use it in sync tests, `await Runner.run(...)` via `asyncio.run(...)` for async/concurrent tests.

- [ ] **Step 1: Write the test module**

```python
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
from openai import AsyncOpenAI

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
    # every event belongs to exactly one run; parent links never cross traces
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
        with pytest.raises(Exception):
            Runner.run_sync(agent, "weather in Paris?")

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
```

Note: the `test_concurrent_runs_keep_traces_separate` parent-link assertion requires every referenced parent to have produced an event in the same trace — if the Agents SDK nests chat spans under an intermediate span that produces no event, relax to "parent_span_id belongs to no OTHER trace's span set" (cross-run bleed is the failure being guarded). Record observed shape either way.

- [ ] **Step 2: Run**

Run: `.venv/bin/python -m pytest honeybadger/tests/contrib/test_llm_agents.py -v`
Expected: PASS. `test_reinit_after_teardown_still_emits` is the singleton regression — it must fail if you revert Task 7's `_release_genai_singleton` (verify once with `git stash` on that hunk if in doubt).

- [ ] **Step 3: Full suite + mypy**

Run: `.venv/bin/python -m pytest honeybadger/tests/ 2>&1 | tail -5` and `.venv/bin/python -m mypy honeybadger/contrib/llm/ 2>&1 | tail -3` (if mypy is configured for the package — match whatever CI runs; see `.github/workflows/`).
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add honeybadger/tests/contrib/test_llm_agents.py
git commit -m "test(llm): openai agents sdk end-to-end integration (tree, concurrency, reinit)"
```

---

### Task 11: Docs — maintainer notes + README

**Files:**
- Modify: `honeybadger/contrib/llm.md`
- Modify: `README.md` (LLM Monitoring section, line ~406)

- [ ] **Step 1: `honeybadger/contrib/llm.md`** — add a "Phase 3: frameworks (LangChain/LangGraph, OpenAI Agents SDK)" section covering, with the empirical findings from the plan header and anything newly observed in Tasks 9–10:
  - The classification table (operation name → event type) and the three new event schemas + run-tree fields added to all span events (`span_id`, `parent_span_id`, span-start `ts`, `conversation_id`).
  - Dedup design: response-identity LRU, first-seen-wins, exclusion-before-insert, honest limits (LRU eviction, cross-batch, pre-instrumented providers), and the **dangling parent pointer** note (the kept provider chat event's `parent_span_id` references the suppressed LangChain twin — join via `trace_id`).
  - The TelemetryHandler singleton lifecycle (why `_release_genai_singleton` exists; LangChain clears it on uninstrument, openai-agents doesn't; foreign-singleton warning).
  - `framework` attribution rules (single-active-framework only; no span attribute exists at this pin).
  - Agents SDK native trace exporter left active by default + `instrument_options={"openai_agents": {"disable_openai_trace_export": True}}` escape hatch; privacy consequence (prompts/tool data flow to OpenAI's trace ingestion regardless of Honeybadger content flags).
  - Sampling: `_hb.sampling_key = trace_id` → whole runs sample atomically; core precedence order.
  - Attribute matrices: one per framework (rows = event fields, columns = scenarios actually tested), cells marked **unverified** when not exercised. Include the known per-pin gaps: LangChain — no agent span without metadata signals, agent metadata suppresses the workflow root, workflow name only in span name; Agents SDK — no `tool_call_id`/`arguments` on tool events, `gen_ai.workflow.name` attr present, no `conversation_id` anywhere.
  - `exclude_models` inapplicability to framework events (documented, per spec §7).
- [ ] **Step 2: README.md** — in "LLM Monitoring (beta)": add frameworks to the supported list ("Agent frameworks: **LangChain/LangGraph** and **OpenAI Agents SDK** — auto-detected"), one sentence on the new event types + run tree (`llm.workflow`/`llm.agent`/`llm.tool_call`, joinable via `trace_id`), the `instrument_options` example with `disable_openai_trace_export` and the privacy note about OpenAI's own trace exporter staying active by default.
- [ ] **Step 3: Update the auto-detect wording** in README ("Auto-detection covers OpenAI and Anthropic only" → "OpenAI, Anthropic, LangChain, and the OpenAI Agents SDK").
- [ ] **Step 4: Commit**

```bash
git add honeybadger/contrib/llm.md README.md
git commit -m "docs(llm): phase-3 framework instrumentation maintainer notes and README"
```

---

## Final verification (before PR)

- [ ] `.venv/bin/python -m pytest honeybadger/tests/` — full suite green.
- [ ] Fresh-venv packaging sanity (per llm.md pin guidance): `python3 -m venv /tmp/hb-llm-check && /tmp/hb-llm-check/bin/pip install -e '.[llm]' 'langchain' 'langgraph' 'langchain-openai' 'openai-agents' -q && /tmp/hb-llm-check/bin/pip check`.
- [ ] Codex review (per user instruction), address findings.
- [ ] PR: base `llm-py-phase2-anthropic-bedrock`, head `llm-py-phase3-frameworks`.
