# LLM Instrumentation Phase 1 (OpenAI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatic OpenAI LLM call monitoring (model, tokens, duration, errors, opt-in prompts/responses) emitting `llm.*` Honeybadger Insights events.

**Architecture:** The official `opentelemetry-instrumentation-genai-openai` package patches the OpenAI SDK and produces GenAI spans on a private Honeybadger `TracerProvider`; a `BatchSpanProcessor` feeds a `HoneybadgerLLMSpanExporter` that normalizes span attributes into `llm.chat`/`llm.embedding` events, applies content policy (part-drop → redact → truncate → byte budget), and pushes through the existing `honeybadger.event()` / `EventsWorker` pipeline. A companion `on_start` processor snapshots `event_context` onto span attributes so correlation survives the thread hop. An `export="otlp"` escape hatch swaps the exporter for a scrubbed stock OTLP exporter.

**Tech Stack:** Python ≥3.10 for the extra (core unchanged), `opentelemetry-sdk`, `opentelemetry-instrumentation-genai-openai`, pytest + unittest.mock, httpx MockTransport for integration tests.

**Authoritative spec:** `docs/superpowers/specs/2026-07-11-llm-instrumentation-design.md`. Where this plan and the spec disagree, the spec wins.

## Global Constraints

- The `[llm]` extra's deps carry env markers `python_version >= "3.10"`; core package deps are unchanged (`psutil`, `six`).
- Extra pins (verbatim from spec, verify at implementation): `opentelemetry-sdk>=1.43,<2`, `opentelemetry-instrumentation-genai-openai>=1.0b0,<1.1`.
- ⚠️ Provenance trap: unprefixed `opentelemetry-instrumentation-anthropic`/`-langchain` on PyPI are Traceloop's. Only `opentelemetry-instrumentation-genai-*` packages are official.
- `honeybadger.contrib.llm` must import cleanly with NO otel deps installed (all otel imports lazy, inside functions).
- `LLMHoneybadger.tearDown()` camel-case (house precedent from the Oban contrib).
- Event field names are frozen by the spec's schema table (e.g. `provider_response_id`, NOT `request_id`; `cache_read_tokens`/`cache_creation_tokens`, NOT `cached_tokens`).
- Fields with no source are omitted, never zero-filled/None-filled.
- Content capture env var: `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`; never override a user-set value; restore on tearDown.
- Run the full existing suite (`pytest honeybadger/tests`) before every commit; pre-commit runs black/pylint.
- Commit messages: conventional commits, `feat(llm):`/`test(llm):`/`docs(llm):` scope, each ending with the Claude co-author trailer used on this branch.

## File Structure

```
honeybadger/contrib/llm/__init__.py    # LLMHoneybadger shell, auto_init(), public surface
honeybadger/contrib/llm/_semconv.py    # versioned current-semconv adapter: span -> NormalizedLLMSpan
honeybadger/contrib/llm/_policy.py     # content policy: part-drop, redact, truncate, byte budget
honeybadger/contrib/llm/_bridge.py     # context processor, events exporter, OTLP scrubbing exporter
honeybadger/contrib/llm.md             # maintainer doc (mirrors contrib/oban.md style)
honeybadger/config.py                  # + LLMConfig, InsightsConfig.llm
honeybadger/utils.py                   # + filter_structure()
honeybadger/contrib/django.py          # + auto_init() call
honeybadger/contrib/flask.py           # + auto_init() call
honeybadger/contrib/asgi.py            # + auto_init() call
setup.py                               # + packages entry, extras_require, python_requires
mypy.ini                               # + opentelemetry.* ignore
dev-requirements.txt                   # + marker-gated otel/openai/httpx test deps
honeybadger/tests/test_utils.py        # + filter_structure tests
honeybadger/tests/test_config.py       # + LLMConfig hydration tests
honeybadger/tests/contrib/test_llm_semconv.py
honeybadger/tests/contrib/test_llm_policy.py
honeybadger/tests/contrib/test_llm.py             # shell + bridge unit tests (fake spans)
honeybadger/tests/contrib/test_llm_integration.py # gated: real instrumentor + mocked transport
examples/llm_app/app.py                # example script against a stub OpenAI server
examples/llm_app/README.md
README.md                              # + LLM Monitoring section
```

Note: the spec names `honeybadger/contrib/llm.py`; this plan uses the package form `honeybadger/contrib/llm/` so `_semconv`/`_policy`/`_bridge` stay focused files. The public import path `from honeybadger.contrib.llm import LLMHoneybadger` is identical.

Fake spans in unit tests use this shared helper (defined in Task 3's test file, imported by later test files):

```python
# honeybadger/tests/contrib/llm_helpers.py  (created in Task 3)
from types import SimpleNamespace

class FakeSpanContext:
    def __init__(self, trace_id=0x1F, span_id=0x2):
        self.trace_id = trace_id
        self.span_id = span_id

class FakeEvent:
    def __init__(self, name, attributes=None):
        self.name = name
        self.attributes = attributes or {}

class FakeStatus:
    def __init__(self, description=None, is_ok=True):
        self.description = description
        self.is_ok = is_ok

class FakeSpan:
    """Duck-types the ReadableSpan surface the bridge reads."""
    def __init__(self, attributes=None, events=None, status=None,
                 start_time=1_000_000_000, end_time=2_234_000_000, name="chat gpt-4o"):
        self.attributes = attributes or {}
        self.events = events or []
        self.status = status or FakeStatus()
        self.start_time = start_time      # ns
        self.end_time = end_time          # ns
        self.name = name
        self._ctx = FakeSpanContext()
    def get_span_context(self):
        return self._ctx
```

---

### Task 1: `filter_structure` list-aware recursive filter

**Files:**
- Modify: `honeybadger/utils.py` (append after `filter_dict`, line 71)
- Test: `honeybadger/tests/test_utils.py` (append)

**Interfaces:**
- Produces: `filter_structure(data, filter_keys) -> Any` — pure function (never mutates input), recurses through dicts AND lists/tuples, replaces values of matching keys with `"[FILTERED]"`, drops tuple keys. Existing `filter_dict` is left untouched (other call sites depend on its in-place behavior).

- [ ] **Step 1: Write the failing tests**

Append to `honeybadger/tests/test_utils.py`:

```python
from honeybadger.utils import filter_structure


def test_filter_structure_filters_keys_inside_lists():
    data = {"messages": [{"role": "user", "password": "hunter2", "content": "hi"}]}
    result = filter_structure(data, ["password"])
    assert result["messages"][0]["password"] == "[FILTERED]"
    assert result["messages"][0]["content"] == "hi"


def test_filter_structure_does_not_mutate_input():
    data = {"outer": [{"password": "hunter2"}]}
    filter_structure(data, ["password"])
    assert data["outer"][0]["password"] == "hunter2"


def test_filter_structure_handles_nested_dicts_and_scalars():
    data = {"a": {"password": "x", "b": [1, "two", {"password": "y"}]}}
    result = filter_structure(data, ["password"])
    assert result["a"]["password"] == "[FILTERED]"
    assert result["a"]["b"][2]["password"] == "[FILTERED]"
    assert result["a"]["b"][0] == 1


def test_filter_structure_drops_tuple_keys():
    data = {("a", "b"): 1, "keep": 2}
    result = filter_structure(data, [])
    assert result == {"keep": 2}


def test_filter_structure_passes_through_non_containers():
    assert filter_structure("plain", ["password"]) == "plain"
    assert filter_structure(None, ["password"]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest honeybadger/tests/test_utils.py -k filter_structure -v`
Expected: FAIL / ERROR with `ImportError: cannot import name 'filter_structure'`

- [ ] **Step 3: Implement**

Append to `honeybadger/utils.py`:

```python
def filter_structure(data, filter_keys):
    """Recursively filter dicts — including dicts inside lists/tuples —
    replacing values of matching keys with "[FILTERED]".

    Unlike filter_dict, this is a pure function: it returns a new
    structure and never mutates the input. Tuple keys are dropped
    (not JSON-serializable).
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if isinstance(key, tuple):
                continue
            if key in filter_keys:
                result[key] = "[FILTERED]"
            else:
                result[key] = filter_structure(value, filter_keys)
        return result
    if isinstance(data, (list, tuple)):
        return [filter_structure(item, filter_keys) for item in data]
    return data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest honeybadger/tests/test_utils.py -v`
Expected: all PASS (new and pre-existing)

- [ ] **Step 5: Commit**

```bash
git add honeybadger/utils.py honeybadger/tests/test_utils.py
git commit --no-gpg-sign -m "feat(llm): add list-aware filter_structure util"
```

---

### Task 2: `LLMConfig` in `insights_config`

**Files:**
- Modify: `honeybadger/config.py` (add dataclass after `CeleryConfig` ~line 68; add field to `InsightsConfig` ~line 71)
- Test: `honeybadger/tests/test_config.py` (append)

**Interfaces:**
- Produces: `honeybadger.config.insights_config.llm` with fields `disabled: bool = False`, `include_prompts: bool = False`, `include_responses: bool = False`, `max_content_length: int = 8192`, `max_event_bytes: int = 65536`, `exclude_models: List[Union[str, Pattern]] = []`.

- [ ] **Step 1: Write the failing tests**

Append to `honeybadger/tests/test_config.py` (match the file's existing test style — it uses plain functions/asserts with `Configuration`):

```python
def test_llm_config_defaults():
    c = Configuration()
    assert c.insights_config.llm.disabled is False
    assert c.insights_config.llm.include_prompts is False
    assert c.insights_config.llm.include_responses is False
    assert c.insights_config.llm.max_content_length == 8192
    assert c.insights_config.llm.max_event_bytes == 65536
    assert c.insights_config.llm.exclude_models == []


def test_llm_config_hydrates_from_dict():
    c = Configuration(
        insights_config={
            "llm": {"include_prompts": True, "max_content_length": 100}
        }
    )
    assert c.insights_config.llm.include_prompts is True
    assert c.insights_config.llm.max_content_length == 100
    # untouched siblings keep defaults
    assert c.insights_config.llm.include_responses is False
    assert c.insights_config.db.disabled is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest honeybadger/tests/test_config.py -k llm -v`
Expected: FAIL with `AttributeError: 'InsightsConfig' object has no attribute 'llm'`

- [ ] **Step 3: Implement**

In `honeybadger/config.py`, after `CeleryConfig` (line 68), add:

```python
@dataclass
class LLMConfig:
    disabled: bool = False
    include_prompts: bool = False
    include_responses: bool = False
    max_content_length: int = 8192  # per content string, chars
    max_event_bytes: int = 65536  # serialized event budget, UTF-8 bytes
    exclude_models: List[Union[str, Pattern]] = field(default_factory=list)
```

In `InsightsConfig`, add the field alongside the others:

```python
    llm: LLMConfig = field(default_factory=LLMConfig)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest honeybadger/tests/test_config.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add honeybadger/config.py honeybadger/tests/test_config.py
git commit --no-gpg-sign -m "feat(llm): add LLMConfig to insights_config"
```

---

### Task 3: `_semconv.py` current-semconv adapter

**Files:**
- Create: `honeybadger/contrib/llm/__init__.py` (empty for now — package marker; real contents in Task 6)
- Create: `honeybadger/contrib/llm/_semconv.py`
- Create: `honeybadger/tests/contrib/llm_helpers.py` (the `FakeSpan` helper from File Structure, verbatim)
- Test: `honeybadger/tests/contrib/test_llm_semconv.py`

**Interfaces:**
- Produces:
  - `NormalizedLLMSpan` dataclass: `event_type: str`, `data: dict`, `prompts: Optional[list]`, `response: Optional[list]`
  - `normalize(span) -> Optional[NormalizedLLMSpan]` — `None` means "not an LLM span, ignore". `data` contains only fields with sources (omit-not-None). `prompts`/`response` are raw decoded message lists (content policy applied later, in `_bridge`).
  - `ADAPTER_VERSION = "genai-1.0"` (string bumped when attribute mappings change)
- Consumes: nothing from other tasks (pure module; no otel imports — operates on duck-typed spans).

- [ ] **Step 1: Create the package marker and test helper**

Create `honeybadger/contrib/llm/__init__.py` containing only:

```python
```

Create `honeybadger/tests/contrib/llm_helpers.py` with the `FakeSpanContext`/`FakeEvent`/`FakeStatus`/`FakeSpan` classes exactly as shown in File Structure above.

- [ ] **Step 2: Write the failing tests**

Create `honeybadger/tests/contrib/test_llm_semconv.py`:

```python
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
    attrs = chat_attributes(**{
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]
        ),
        "gen_ai.output.messages": json.dumps(
            [{"role": "assistant", "parts": [{"type": "text", "content": "hello"}]}]
        ),
        "gen_ai.system_instructions": json.dumps(
            [{"type": "text", "content": "be brief"}]
        ),
    })
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
    attrs = chat_attributes(**{
        "gen_ai.usage.cache_read.input_tokens": 7,
        "gen_ai.usage.cache_creation.input_tokens": 3,
    })
    n = normalize(FakeSpan(attributes=attrs))
    assert n.data["cache_read_tokens"] == 7
    assert n.data["cache_creation_tokens"] == 3


def test_normalize_error_extraction_order():
    # 1) error.type attribute wins
    n = normalize(FakeSpan(attributes=chat_attributes(**{"error.type": "RateLimitError"})))
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
    emb = FakeSpan(attributes={
        "gen_ai.operation.name": "embeddings",
        "gen_ai.request.model": "text-embedding-3-small",
        "gen_ai.usage.input_tokens": 5,
    })
    assert normalize(emb).event_type == "llm.embedding"

    unknown = FakeSpan(attributes={"gen_ai.operation.name": "moderation"})
    assert normalize(unknown).event_type == "llm.call"

    not_llm = FakeSpan(attributes={"http.method": "GET"})
    assert normalize(not_llm) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest honeybadger/tests/contrib/test_llm_semconv.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'honeybadger.contrib.llm._semconv'`

- [ ] **Step 4: Implement**

Create `honeybadger/contrib/llm/_semconv.py`:

```python
"""Versioned adapter: current OTel GenAI semconv span -> normalized event fields.

All attribute knowledge for the "events" export mode lives here. Every
attribute is optional; absent sources mean absent fields (never None).
No opentelemetry imports — operates on the ReadableSpan duck-type
(attributes, events, status, start_time, end_time, get_span_context).
"""
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

ADAPTER_VERSION = "genai-1.0"

_OPERATION_EVENT_TYPES = {
    "chat": "llm.chat",
    "embeddings": "llm.embedding",
    "embedding": "llm.embedding",
}

# metadata attribute -> event field (direct copies)
_SCALAR_FIELDS = {
    "gen_ai.provider.name": "provider",
    "gen_ai.system": "provider",  # legacy fallback; provider.name wins (ordered dict)
    "server.address": "host",
    "gen_ai.request.model": "model",
    "gen_ai.response.model": "response_model",
    "gen_ai.usage.input_tokens": "input_tokens",
    "gen_ai.usage.output_tokens": "output_tokens",
    "gen_ai.usage.cache_read.input_tokens": "cache_read_tokens",
    "gen_ai.usage.cache_creation.input_tokens": "cache_creation_tokens",
    "gen_ai.request.temperature": "temperature",
    "gen_ai.response.id": "provider_response_id",
}


@dataclass
class NormalizedLLMSpan:
    event_type: str
    data: Dict[str, Any]
    prompts: Optional[List[dict]]
    response: Optional[List[dict]]


def normalize(span) -> Optional[NormalizedLLMSpan]:
    attributes = dict(span.attributes or {})
    if not any(key.startswith("gen_ai.") for key in attributes):
        return None

    operation = attributes.get("gen_ai.operation.name")
    event_type = _OPERATION_EVENT_TYPES.get(operation, "llm.call")

    data: Dict[str, Any] = {}
    for attr, field_name in _SCALAR_FIELDS.items():
        if attr in attributes and field_name not in data:
            data[field_name] = attributes[attr]

    finish_reasons = attributes.get("gen_ai.response.finish_reasons")
    if finish_reasons:
        data["finish_reason"] = list(finish_reasons)[0]

    duration = _duration_ms(span)
    if duration is not None:
        data["duration"] = duration

    trace_id = _trace_id(span)
    if trace_id:
        data["trace_id"] = trace_id

    error = _extract_error(span, attributes)
    if error:
        data["error"] = error

    prompts = _decode_messages(attributes.get("gen_ai.input.messages"))
    system = _decode_system_instructions(attributes.get("gen_ai.system_instructions"))
    if system:
        prompts = [{"role": "system", "content": system}] + (prompts or [])
    response = _decode_messages(attributes.get("gen_ai.output.messages"))

    return NormalizedLLMSpan(
        event_type=event_type, data=data, prompts=prompts, response=response
    )


def _duration_ms(span) -> Optional[int]:
    start, end = getattr(span, "start_time", None), getattr(span, "end_time", None)
    if start is None or end is None:
        return None
    return int((end - start) / 1_000_000)


def _trace_id(span) -> Optional[str]:
    try:
        return format(span.get_span_context().trace_id, "032x")
    except Exception:
        return None


def _extract_error(span, attributes) -> Optional[str]:
    # Order per spec: error.type attr -> exception event -> status description.
    error_type = attributes.get("error.type")
    if error_type:
        return str(error_type)
    for event in getattr(span, "events", None) or []:
        if event.name == "exception":
            exc_type = (event.attributes or {}).get("exception.type")
            if exc_type:
                return str(exc_type)
    status = getattr(span, "status", None)
    if status is not None and not getattr(status, "is_ok", True):
        return getattr(status, "description", None) or None
    return None


def _decode_messages(raw) -> Optional[List[dict]]:
    """gen_ai.{input,output}.messages are JSON-encoded strings:
    [{"role": ..., "parts": [{"type": "text", "content": ...}, ...]}, ...]
    Flatten to [{role, content}] where content is the text parts (str) or,
    for multi/non-text parts, the raw parts list (content policy handles it).
    """
    if not raw:
        return None
    try:
        messages = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(messages, list):
        return None
    result = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "unknown")
        parts = message.get("parts")
        if parts is None:
            content = message.get("content")
        else:
            content = _flatten_parts(parts)
        result.append({"role": role, "content": content})
    return result or None


def _flatten_parts(parts):
    if not isinstance(parts, list):
        return parts
    texts = [
        part.get("content")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    if len(texts) == len(parts):
        return "\n".join(str(text) for text in texts)
    return parts  # mixed/non-text: leave for content policy to part-drop


def _decode_system_instructions(raw) -> Optional[str]:
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            parts = json.loads(raw)
        except ValueError:
            return raw  # plain string instructions
    else:
        parts = raw
    flattened = _flatten_parts(parts if isinstance(parts, list) else [parts])
    return flattened if isinstance(flattened, str) else None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest honeybadger/tests/contrib/test_llm_semconv.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add honeybadger/contrib/llm/ honeybadger/tests/contrib/llm_helpers.py honeybadger/tests/contrib/test_llm_semconv.py
git commit --no-gpg-sign -m "feat(llm): add current-semconv normalization adapter"
```

---

### Task 4: `_policy.py` content policy pipeline

**Files:**
- Create: `honeybadger/contrib/llm/_policy.py`
- Test: `honeybadger/tests/contrib/test_llm_policy.py`

**Interfaces:**
- Consumes: `filter_structure` from Task 1.
- Produces:
  - `apply_content_policy(messages, filter_keys, max_content_length) -> Optional[list]` — pure; normative order part-drop → redact → truncate; returns new list.
  - `enforce_event_budget(data, max_event_bytes) -> dict` — serialized (UTF-8 JSON) size capped; drops `prompts` messages oldest-first keeping a leading system message and `response`; sets `data["content_dropped"] = True` when it drops; returns (possibly same) dict.
  - `TRUNCATION_MARKER = "... [TRUNCATED]"`, `OMITTED_PART = "[non-text content omitted]"`

- [ ] **Step 1: Write the failing tests**

Create `honeybadger/tests/contrib/test_llm_policy.py`:

```python
import json

from honeybadger.contrib.llm._policy import (
    apply_content_policy,
    enforce_event_budget,
    TRUNCATION_MARKER,
    OMITTED_PART,
)


def test_policy_drops_non_text_parts():
    messages = [{"role": "user", "content": [
        {"type": "text", "content": "look at this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
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
    assert result["prompts"][0]["role"] == "system"       # kept
    assert result["response"][0]["content"] == "answer"   # kept
    assert len(result["prompts"]) < 11
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 900


def test_budget_metadata_only_event_is_untouched():
    data = {"provider": "openai", "model": "gpt-4o"}
    assert enforce_event_budget(dict(data), 10) == data  # nothing droppable
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest honeybadger/tests/contrib/test_llm_policy.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'honeybadger.contrib.llm._policy'`

- [ ] **Step 3: Implement**

Create `honeybadger/contrib/llm/_policy.py`:

```python
"""Content policy for LLM events. Normative order (spec):
part-drop -> structural redaction -> per-string truncation -> byte budget.
All functions are pure with respect to their message inputs.
"""
import json
from typing import List, Optional

from honeybadger.utils import filter_structure

TRUNCATION_MARKER = "... [TRUNCATED]"
OMITTED_PART = "[non-text content omitted]"


def apply_content_policy(
    messages: Optional[list], filter_keys, max_content_length: int
) -> Optional[list]:
    if messages is None:
        return None
    dropped = [_drop_non_text(dict(message)) for message in messages]
    redacted = filter_structure(dropped, filter_keys)
    return [_truncate_message(message, max_content_length) for message in redacted]


def _drop_non_text(message: dict) -> dict:
    content = message.get("content")
    if isinstance(content, list):
        message["content"] = [
            part if isinstance(part, str)
            else (part.get("content") if isinstance(part, dict) and part.get("type") == "text"
                  else OMITTED_PART)
            for part in content
        ]
    return message


def _truncate_message(message, max_length: int):
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if isinstance(content, str) and len(content) > max_length:
        message["content"] = content[:max_length] + TRUNCATION_MARKER
    elif isinstance(content, list):
        message["content"] = [
            part[:max_length] + TRUNCATION_MARKER
            if isinstance(part, str) and len(part) > max_length
            else part
            for part in content
        ]
    return message


def _size(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False, default=repr).encode("utf-8"))


def enforce_event_budget(data: dict, max_event_bytes: int) -> dict:
    """Drop prompt messages oldest-first (keeping one leading system message)
    until the serialized event fits. The response is preserved. Sets
    content_dropped when anything was removed."""
    if _size(data) <= max_event_bytes:
        return data

    prompts = data.get("prompts")
    if not isinstance(prompts, list) or not prompts:
        return data  # nothing droppable; EventsWorker/API limits are the backstop

    keep_system = prompts[0] if prompts and prompts[0].get("role") == "system" else None
    droppable = prompts[1:] if keep_system else list(prompts)
    dropped_any = False
    while droppable and _size(data) > max_event_bytes:
        droppable.pop(0)
        dropped_any = True
        data["prompts"] = ([keep_system] if keep_system else []) + droppable
    if dropped_any:
        data["content_dropped"] = True
    return data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest honeybadger/tests/contrib/test_llm_policy.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add honeybadger/contrib/llm/_policy.py honeybadger/tests/contrib/test_llm_policy.py
git commit --no-gpg-sign -m "feat(llm): add content policy pipeline (part-drop, redact, truncate, budget)"
```

---

### Task 5: `_bridge.py` — context processor + events exporter

**Files:**
- Create: `honeybadger/contrib/llm/_bridge.py`
- Test: `honeybadger/tests/contrib/test_llm.py` (bridge portion)

**Interfaces:**
- Consumes: `normalize` (Task 3), `apply_content_policy`/`enforce_event_budget` (Task 4).
- Produces:
  - `CONTEXT_ATTR_PREFIX = "honeybadger.context."`
  - `snapshot_context_attributes(span) -> None` — reads `honeybadger._get_event_context()`, sets scalar values as `honeybadger.context.<key>` span attributes. Called from `on_start` on the calling thread.
  - `HoneybadgerContextSpanProcessor` — otel `SpanProcessor` subclass wrapping `snapshot_context_attributes` (constructed lazily in Task 6; `_bridge` defines a factory `make_context_processor()` that imports otel inside the function).
  - `export_spans(spans, owner) -> None` — pure-python core used by the exporter: for each span, gate on `owner.active` (bool), `insights_enabled`, `llm.disabled`; normalize; exclude; policy; budget; context-lift; `honeybadger.event()`. Never raises.
  - `make_events_exporter(owner)` / `make_otlp_exporter(owner, wrapped)` — lazy factories returning otel `SpanExporter` instances (Task 6 wires them; OTLP scrubbing in Task 8).
  - `owner` duck-type: object with `.active: bool` (False after tearDown → inert).
- The split between pure `export_spans` and the lazy otel-subclass factories is what keeps this module importable without otel.

- [ ] **Step 1: Write the failing tests**

Create `honeybadger/tests/contrib/test_llm.py` (bridge section; the file grows in Tasks 6–8):

```python
import json
import re
from types import SimpleNamespace
from unittest.mock import patch

from honeybadger import honeybadger
from honeybadger.contrib.llm import _bridge
from honeybadger.contrib.llm._bridge import (
    CONTEXT_ATTR_PREFIX,
    snapshot_context_attributes,
    export_spans,
)
from honeybadger.tests.contrib.llm_helpers import FakeSpan


class RecordingSpan(FakeSpan):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_attributes = {}

    def set_attribute(self, key, value):
        self.set_attributes[key] = value


def owner(active=True):
    return SimpleNamespace(active=active)


def configured(**llm_overrides):
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": llm_overrides},
    )


def chat_span(**attr_overrides):
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 12,
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]
        ),
        "gen_ai.output.messages": json.dumps(
            [{"role": "assistant", "parts": [{"type": "text", "content": "hello"}]}]
        ),
    }
    attrs.update(attr_overrides)
    return FakeSpan(attributes=attrs)


def teardown_function():
    honeybadger.reset_event_context()


# --- context snapshot ---

def test_snapshot_copies_scalar_event_context_onto_span():
    honeybadger.set_event_context(request_id="req-1", user_id=42, nested={"a": 1})
    span = RecordingSpan()
    snapshot_context_attributes(span)
    assert span.set_attributes[CONTEXT_ATTR_PREFIX + "request_id"] == "req-1"
    assert span.set_attributes[CONTEXT_ATTR_PREFIX + "user_id"] == 42
    assert CONTEXT_ATTR_PREFIX + "nested" not in span.set_attributes  # scalars only


# --- export_spans ---

def test_export_emits_llm_chat_event_with_context_lift():
    configured()
    span = chat_span()
    span.attributes[CONTEXT_ATTR_PREFIX + "request_id"] = "req-9"
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([span], owner())
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"
    assert data["provider"] == "openai"
    assert data["request_id"] == "req-9"
    assert "prompts" not in data       # include_prompts defaults off
    assert "response" not in data


def test_export_includes_content_when_opted_in():
    configured(include_prompts=True, include_responses=True)
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner())
    data = mock_event.call_args[0][1]
    assert data["prompts"] == [{"role": "user", "content": "hi"}]
    assert data["response"] == [{"role": "assistant", "content": "hello"}]


def test_export_respects_independent_flags():
    configured(include_prompts=True, include_responses=False)
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner())
    data = mock_event.call_args[0][1]
    assert "prompts" in data and "response" not in data


def test_export_skips_when_disabled_or_inactive_or_not_llm():
    configured(disabled=True)
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner())                       # disabled
        mock_event.assert_not_called()
    configured()
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner(active=False))           # torn down
        export_spans([FakeSpan(attributes={"http.method": "GET"})], owner())
        mock_event.assert_not_called()


def test_export_exclude_models_exact_string_and_regex():
    configured(exclude_models=["gpt-4o", re.compile(r"^o1-")])
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([chat_span()], owner())                                   # exact
        export_spans([chat_span(**{"gen_ai.request.model": "o1-mini"})], owner())  # regex
        mock_event.assert_not_called()
    with patch.object(honeybadger, "event") as mock_event:
        # substring must NOT match ("gpt-4" is not "gpt-4o")
        export_spans([chat_span(**{"gen_ai.request.model": "gpt-4"})], owner())
        mock_event.assert_called_once()


def test_export_never_raises_on_malformed_span():
    configured()
    class ExplodingSpan:
        @property
        def attributes(self):
            raise RuntimeError("boom")
    with patch.object(honeybadger, "event") as mock_event:
        export_spans([ExplodingSpan(), chat_span()], owner())  # survives, continues
        mock_event.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest honeybadger/tests/contrib/test_llm.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'honeybadger.contrib.llm._bridge'`

- [ ] **Step 3: Implement**

Create `honeybadger/contrib/llm/_bridge.py`:

```python
"""Span -> Honeybadger event bridge.

export_spans() is pure Python (no otel imports) so it unit-tests against
duck-typed spans and the module imports without the [llm] extra. The
make_*() factories build real otel SpanProcessor/SpanExporter subclasses
and import opentelemetry lazily inside the function bodies.
"""
import logging

from honeybadger import honeybadger
from ._semconv import normalize
from ._policy import apply_content_policy, enforce_event_budget

logger = logging.getLogger(__name__)

CONTEXT_ATTR_PREFIX = "honeybadger.context."

_warned_failure_classes = set()


def snapshot_context_attributes(span):
    """Copy scalar event-context values onto the span (calling thread)."""
    try:
        context = honeybadger._get_event_context() or {}
        for key, value in context.items():
            if isinstance(value, (str, int, float, bool)):
                span.set_attribute(CONTEXT_ATTR_PREFIX + str(key), value)
    except Exception as exc:  # never break span start
        _warn_once("context_snapshot", exc)


def export_spans(spans, owner):
    for span in spans:
        try:
            _export_one(span, owner)
        except Exception as exc:
            _warn_once("export", exc)


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

    if llm_config.include_prompts and normalized.prompts is not None:
        data["prompts"] = apply_content_policy(
            normalized.prompts, config.params_filters, llm_config.max_content_length
        )
    if llm_config.include_responses and normalized.response is not None:
        data["response"] = apply_content_policy(
            normalized.response, config.params_filters, llm_config.max_content_length
        )
    data = enforce_event_budget(data, llm_config.max_event_bytes)

    for key, value in (span.attributes or {}).items():
        if key.startswith(CONTEXT_ATTR_PREFIX):
            data.setdefault(key[len(CONTEXT_ATTR_PREFIX):], value)

    honeybadger.event(normalized.event_type, data)


def _excluded(model, exclude_models):
    if not model:
        return False
    for pattern in exclude_models:
        if hasattr(pattern, "search"):
            if pattern.search(model):
                return True
        elif pattern == model:
            return True
    return False


def _warn_once(failure_class, exc):
    if failure_class not in _warned_failure_classes:
        _warned_failure_classes.add(failure_class)
        logger.warning("honeybadger llm bridge %s failure: %s", failure_class, exc)
    else:
        logger.debug("honeybadger llm bridge %s failure: %s", failure_class, exc)


def make_context_processor():
    from opentelemetry.sdk.trace import SpanProcessor

    class HoneybadgerContextSpanProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            snapshot_context_attributes(span)

    return HoneybadgerContextSpanProcessor()


def make_events_exporter(owner):
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class HoneybadgerLLMSpanExporter(SpanExporter):
        def export(self, spans):
            export_spans(spans, owner)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    return HoneybadgerLLMSpanExporter()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest honeybadger/tests/contrib/test_llm.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add honeybadger/contrib/llm/_bridge.py honeybadger/tests/contrib/test_llm.py
git commit --no-gpg-sign -m "feat(llm): add span-to-event bridge with context snapshot"
```

---

### Task 6: `LLMHoneybadger` shell + `auto_init`

**Files:**
- Modify: `honeybadger/contrib/llm/__init__.py` (replace empty file)
- Test: `honeybadger/tests/contrib/test_llm.py` (append shell section)

**Interfaces:**
- Consumes: `make_context_processor`, `make_events_exporter`, `export_spans` (Task 5).
- Produces (public API, frozen for later tasks):
  - `LLMHoneybadger(instruments=None, tracer_provider=None, export="events")`
  - `.init()` / `.tearDown()` — idempotent; single-active-instance guard (`RuntimeError`); `.active` property read by the bridge owner protocol.
  - `auto_init() -> Optional[LLMHoneybadger]` — module-level shared instance; silent no-op when otel deps missing, when llm config disabled, when insights disabled, or when an instance is already active.
  - `CONTENT_ENV_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"`
  - Instrument registry: `_INSTRUMENTORS = {"openai": ...}` keyed by provider key.

- [ ] **Step 1: Write the failing tests**

Append to `honeybadger/tests/contrib/test_llm.py`:

```python
import os
import pytest

import honeybadger.contrib.llm as llm_module
from honeybadger.contrib.llm import LLMHoneybadger, auto_init, CONTENT_ENV_VAR


@pytest.fixture(autouse=True)
def reset_llm_state(monkeypatch):
    yield
    if llm_module._active_instance is not None:
        llm_module._active_instance.tearDown()
    llm_module._auto_instance = None
    os.environ.pop(CONTENT_ENV_VAR, None)


def test_module_imports_without_otel():
    # The suite itself may not have otel installed on this row; importing
    # the module and constructing the class must always work.
    instance = LLMHoneybadger()
    assert instance.active is False


def test_init_without_deps_raises_importerror(monkeypatch):
    monkeypatch.setattr(llm_module, "_otel_available", lambda: False)
    with pytest.raises(ImportError) as excinfo:
        LLMHoneybadger().init()
    assert "honeybadger[llm]" in str(excinfo.value)


def _fake_otel(monkeypatch, instance_holder=None):
    """Stub the otel-touching seams so init() runs without the extra."""
    monkeypatch.setattr(llm_module, "_otel_available", lambda: True)
    fake_provider = SimpleNamespace(
        added=[],
        add_span_processor=lambda self=None, p=None: fake_provider.added.append(p),
        shutdown=lambda: fake_provider.added.append("shutdown"),
        force_flush=lambda timeout_millis=30000: True,
    )
    # add_span_processor defined via closure to allow single-arg call
    fake_provider.add_span_processor = fake_provider.added.append
    monkeypatch.setattr(llm_module, "_build_provider", lambda: fake_provider)
    monkeypatch.setattr(llm_module, "_attach_pipeline", lambda self, provider: None)
    instrumented = []
    monkeypatch.setattr(
        llm_module,
        "_activate_instrumentors",
        lambda self, provider: instrumented.append("openai") or ["openai"],
    )
    monkeypatch.setattr(llm_module, "_deactivate_instrumentors", lambda self: None)
    return fake_provider, instrumented


def test_init_teardown_lifecycle_and_guard(monkeypatch):
    _fake_otel(monkeypatch)
    first = LLMHoneybadger()
    first.init()
    assert first.active is True
    first.init()  # idempotent
    second = LLMHoneybadger()
    with pytest.raises(RuntimeError):
        second.init()
    first.tearDown()
    assert first.active is False
    second.init()  # guard released
    second.tearDown()


def test_env_gating_set_and_restored(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(
        api_key="fake", insights_enabled=True,
        insights_config={"llm": {"include_prompts": True}},
    )
    instance = LLMHoneybadger()
    instance.init()
    assert os.environ[CONTENT_ENV_VAR] == "span_only"
    instance.tearDown()
    assert CONTENT_ENV_VAR not in os.environ


def test_env_gating_never_overrides_user(monkeypatch):
    _fake_otel(monkeypatch)
    os.environ[CONTENT_ENV_VAR] = "no_content"
    honeybadger.configure(
        api_key="fake", insights_enabled=True,
        insights_config={"llm": {"include_prompts": True}},
    )
    instance = LLMHoneybadger()
    instance.init()
    assert os.environ[CONTENT_ENV_VAR] == "no_content"
    instance.tearDown()
    assert os.environ[CONTENT_ENV_VAR] == "no_content"


def test_env_gating_left_unset_when_content_off(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(
        api_key="fake", insights_enabled=True, insights_config={"llm": {}}
    )
    instance = LLMHoneybadger()
    instance.init()
    assert CONTENT_ENV_VAR not in os.environ
    instance.tearDown()


def test_invalid_export_value_rejected():
    with pytest.raises(ValueError):
        LLMHoneybadger(export="carrier-pigeon")


def test_auto_init_is_silent_without_deps(monkeypatch):
    monkeypatch.setattr(llm_module, "_otel_available", lambda: False)
    assert auto_init() is None  # no raise


def test_auto_init_shares_one_instance(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(api_key="fake", insights_enabled=True)
    first = auto_init()
    second = auto_init()
    assert first is second and first.active


def test_auto_init_respects_disabled(monkeypatch):
    _fake_otel(monkeypatch)
    honeybadger.configure(
        api_key="fake", insights_enabled=True,
        insights_config={"llm": {"disabled": True}},
    )
    assert auto_init() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest honeybadger/tests/contrib/test_llm.py -v`
Expected: new tests ERROR with `ImportError: cannot import name 'LLMHoneybadger'`

- [ ] **Step 3: Implement**

Replace `honeybadger/contrib/llm/__init__.py`:

```python
"""Honeybadger LLM instrumentation (phase 1: OpenAI).

Spec: docs/superpowers/specs/2026-07-11-llm-instrumentation-design.md
Maintainer notes: honeybadger/contrib/llm.md
"""
import importlib.util
import logging
import os
import threading

from honeybadger import honeybadger
from . import _bridge

logger = logging.getLogger(__name__)

CONTENT_ENV_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
_EXPORT_MODES = ("events", "otlp")

# provider key -> (sdk module to detect, instrumentor module, instrumentor class)
_INSTRUMENTORS = {
    "openai": (
        "openai",
        "opentelemetry.instrumentation.genai.openai",
        "OpenAIInstrumentor",
    ),
}

_active_instance = None
_auto_instance = None
_lock = threading.Lock()


def _otel_available():
    return (
        importlib.util.find_spec("opentelemetry.sdk") is not None
        and importlib.util.find_spec("opentelemetry.instrumentation.genai.openai")
        is not None
    )


def _build_provider():
    from opentelemetry.sdk.trace import TracerProvider

    return TracerProvider()


def _attach_pipeline(self, provider):
    """Attach context processor + exporter pipeline to the provider."""
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        SimpleSpanProcessor,
    )

    provider.add_span_processor(_bridge.make_context_processor())
    exporter = self._build_exporter()
    # Lambda: batch thread may be frozen between invocations; go synchronous.
    if honeybadger.config.is_aws_lambda_environment:
        processor = SimpleSpanProcessor(exporter)
    else:
        processor = BatchSpanProcessor(exporter)
    self._processor = processor
    provider.add_span_processor(processor)


def _activate_instrumentors(self, provider):
    """Instrument each requested/detected provider we can own. Returns keys."""
    import importlib

    activated = []
    for key in self._requested_instruments():
        sdk_module, module_name, class_name = _INSTRUMENTORS[key]
        if importlib.util.find_spec(sdk_module) is None:
            continue
        instrumentor_cls = getattr(importlib.import_module(module_name), class_name)
        instrumentor = instrumentor_cls()
        if instrumentor.is_instrumented_by_opentelemetry:
            logger.warning(
                "honeybadger llm: %s already instrumented by another consumer; skipping",
                key,
            )
            continue
        instrumentor.instrument(tracer_provider=provider)
        self._instrumentors[key] = instrumentor
        activated.append(key)
    return activated


def _deactivate_instrumentors(self):
    for key, instrumentor in list(self._instrumentors.items()):
        try:
            instrumentor.uninstrument()
        except Exception as exc:
            logger.warning("honeybadger llm: uninstrument %s failed: %s", key, exc)
        self._instrumentors.pop(key, None)


class LLMHoneybadger(object):
    def __init__(self, instruments=None, tracer_provider=None, export="events"):
        if export not in _EXPORT_MODES:
            raise ValueError(
                "export must be one of %r, got %r" % (_EXPORT_MODES, export)
            )
        self.instruments = instruments
        self.export = export
        self._borrowed_provider = tracer_provider
        self._provider = None
        self._processor = None
        self._instrumentors = {}
        self._initialized = False
        self._env_was_set_by_us = False

    @property
    def active(self):
        return self._initialized

    def _requested_instruments(self):
        if self.instruments is not None:
            unknown = set(self.instruments) - set(_INSTRUMENTORS)
            if unknown:
                raise ValueError("unknown instruments: %s" % sorted(unknown))
            return list(self.instruments)
        return list(_INSTRUMENTORS)

    def init(self):
        global _active_instance
        if self._initialized:
            return self
        with _lock:
            if _active_instance is not None and _active_instance is not self:
                raise RuntimeError(
                    "another LLMHoneybadger instance is active; tearDown() it first"
                )
            _active_instance = self
        try:
            if not _otel_available():
                raise ImportError(
                    "LLM instrumentation requires the [llm] extra on Python >= 3.10: "
                    "pip install 'honeybadger[llm]'"
                )
            self._apply_env_gating()
            provider = self._borrowed_provider or _build_provider()
            _attach_pipeline(self, provider)
            self._provider = provider
            _activate_instrumentors(self, provider)
            self._initialized = True
        except Exception:
            self._cleanup_wiring()
            with _lock:
                _active_instance = None
            raise
        return self

    def tearDown(self):
        global _active_instance
        if not self._initialized and _active_instance is not self:
            return
        self._initialized = False
        self._cleanup_wiring()
        with _lock:
            if _active_instance is self:
                _active_instance = None

    def _apply_env_gating(self):
        # Before instrumenting: never override a user-set value.
        if CONTENT_ENV_VAR in os.environ:
            return
        llm_config = honeybadger.config.insights_config.llm
        if llm_config.include_prompts or llm_config.include_responses:
            os.environ[CONTENT_ENV_VAR] = "span_only"
            self._env_was_set_by_us = True

    def _restore_env_gating(self):
        if self._env_was_set_by_us:
            os.environ.pop(CONTENT_ENV_VAR, None)
            self._env_was_set_by_us = False

    def _build_exporter(self):
        if self.export == "otlp":
            return _bridge.make_otlp_exporter(self)
        return _bridge.make_events_exporter(self)

    def _cleanup_wiring(self):
        _deactivate_instrumentors(self)
        self._restore_env_gating()
        if self._provider is not None and self._borrowed_provider is None:
            # Owned provider: flush + shutdown. Borrowed: leave attached,
            # exporter goes inert via self.active (no remove_span_processor API).
            try:
                self._provider.force_flush()
                self._provider.shutdown()
            except Exception as exc:
                logger.debug("honeybadger llm: provider shutdown failed: %s", exc)
        self._provider = None
        self._processor = None


def auto_init():
    """Shared-instance init used by framework integrations. Never raises."""
    global _auto_instance
    try:
        if not _otel_available():
            return None
        config = honeybadger.config
        if not config.insights_enabled or config.insights_config.llm.disabled:
            return None
        if _active_instance is not None:
            return _active_instance if _active_instance is _auto_instance else None
        _auto_instance = LLMHoneybadger()
        _auto_instance.init()
        return _auto_instance
    except Exception as exc:
        logger.debug("honeybadger llm auto_init skipped: %s", exc)
        _auto_instance = None
        return None
```

Note for the implementer: `_attach_pipeline`, `_activate_instrumentors`, `_deactivate_instrumentors`, `_build_provider`, and `_otel_available` are module-level functions (not methods) so tests can monkeypatch them as seams; `LLMHoneybadger` calls them as `_attach_pipeline(self, provider)` etc. `make_otlp_exporter` does not exist until Task 8 — that is fine because `export="otlp"` is only exercised in Task 8's tests.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest honeybadger/tests/contrib/test_llm.py -v`
Expected: all PASS. Also run the whole suite: `pytest honeybadger/tests` — no regressions.

- [ ] **Step 5: Commit**

```bash
git add honeybadger/contrib/llm/__init__.py honeybadger/tests/contrib/test_llm.py
git commit --no-gpg-sign -m "feat(llm): add LLMHoneybadger contrib shell with auto_init"
```

---

### Task 7: Framework auto-init wiring (Django, Flask, ASGI)

**Files:**
- Modify: `honeybadger/contrib/django.py:139-141` (`DjangoHoneybadgerMiddleware.__init__`)
- Modify: `honeybadger/contrib/flask.py:148` (`FlaskHoneybadger.init_app`, inside the `insights_enabled` branch)
- Modify: `honeybadger/contrib/asgi.py:99` (`ASGIHoneybadger.__init__`, after `honeybadger.configure`)
- Test: `honeybadger/tests/contrib/test_llm.py` (append)

**Interfaces:**
- Consumes: `auto_init` from Task 6 (`from honeybadger.contrib.llm import auto_init` — import at call site, lazy, so these modules add no import-time dependency).

- [ ] **Step 1: Write the failing test**

Append to `honeybadger/tests/contrib/test_llm.py`:

```python
def test_django_middleware_calls_auto_init(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_module, "auto_init", lambda: calls.append(1))
    import django
    from django.conf import settings as django_settings
    if not django_settings.configured:
        django_settings.configure(DEBUG=False, HONEYBADGER={"API_KEY": "fake"})
        django.setup()
    monkeypatch.setattr(
        type(honeybadger.config), "insights_enabled", True, raising=False
    )
    from honeybadger.contrib.django import DjangoHoneybadgerMiddleware
    with patch.object(DjangoHoneybadgerMiddleware, "_patch_cursor"):
        DjangoHoneybadgerMiddleware(get_response=lambda request: None)
    assert calls == [1]
```

(Follow the established Django test bootstrap in `honeybadger/tests/contrib/test_django.py` — reuse its settings/configure fixture pattern rather than the sketch above if it differs. Flask and ASGI wiring is one line each mirroring Django; cover Django in a unit test and verify Flask/ASGI via the same-line code review + example app rather than duplicating framework bootstraps.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest honeybadger/tests/contrib/test_llm.py -k auto_init -v`
Expected: the Django test FAILS (`calls == []`)

- [ ] **Step 3: Implement the three one-line hooks**

`honeybadger/contrib/django.py` — in `DjangoHoneybadgerMiddleware.__init__`, extend the existing insights branch (lines 140-141):

```python
        if honeybadger.config.insights_enabled:
            self._patch_cursor()
            from honeybadger.contrib.llm import auto_init

            auto_init()
```

`honeybadger/contrib/flask.py` — in `init_app`, inside the `if honeybadger.config.insights_enabled:` branch at line 148, append:

```python
            from honeybadger.contrib.llm import auto_init

            auto_init()
```

`honeybadger/contrib/asgi.py` — in `ASGIHoneybadger.__init__`, after the `honeybadger.configure(**kwargs)` call (line 99), append:

```python
        if honeybadger.config.insights_enabled:
            from honeybadger.contrib.llm import auto_init

            auto_init()
```

- [ ] **Step 4: Run tests**

Run: `pytest honeybadger/tests/contrib/ -v`
Expected: all PASS, including the pre-existing django/flask/asgi suites.

- [ ] **Step 5: Commit**

```bash
git add honeybadger/contrib/django.py honeybadger/contrib/flask.py honeybadger/contrib/asgi.py honeybadger/tests/contrib/test_llm.py
git commit --no-gpg-sign -m "feat(llm): auto-init LLM instrumentation from framework integrations"
```

---

### Task 8: `export="otlp"` scrubbing exporter

**Files:**
- Modify: `honeybadger/contrib/llm/_bridge.py` (append)
- Test: `honeybadger/tests/contrib/test_llm.py` (append)

**Interfaces:**
- Consumes: `apply_content_policy` semantics via `scrub_attributes` (new pure function), `owner.active`, `LLMConfig`.
- Produces:
  - `scrub_attributes(attributes, llm_config, params_filters) -> Optional[dict]` — pure. Returns `None` when the span must be dropped entirely (excluded model / disabled). Otherwise a NEW attributes dict: content attributes (`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`) removed unless the matching include flag is on; when kept, their JSON is decoded, run through the content policy, re-encoded, and truncated.
  - `make_otlp_exporter(owner)` — lazy factory; raises `ImportError` naming `opentelemetry-exporter-otlp-proto-http` when missing; wraps `OTLPSpanExporter(endpoint=config.endpoint + "/v1/traces", headers={"X-API-Key": config.api_key})` with a scrubbing exporter that rebuilds each span with `scrub_attributes` output and drops `None`s.

- [ ] **Step 1: Write the failing tests**

Append to `honeybadger/tests/contrib/test_llm.py`:

```python
from honeybadger.contrib.llm._bridge import scrub_attributes
from honeybadger.config import LLMConfig


def test_scrub_drops_content_attrs_by_default():
    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.input.messages": json.dumps([{"role": "user", "parts": []}]),
        "gen_ai.output.messages": json.dumps([{"role": "assistant", "parts": []}]),
    }
    result = scrub_attributes(attrs, LLMConfig(), ["password"])
    assert "gen_ai.input.messages" not in result
    assert "gen_ai.output.messages" not in result
    assert result["gen_ai.request.model"] == "gpt-4o"
    assert attrs["gen_ai.input.messages"]  # input untouched


def test_scrub_keeps_and_redacts_content_when_opted_in():
    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "content": "hi", "password": "hunter2"}]
        ),
    }
    config = LLMConfig(include_prompts=True)
    result = scrub_attributes(attrs, config, ["password"])
    decoded = json.loads(result["gen_ai.input.messages"])
    assert decoded[0]["password"] == "[FILTERED]"


def test_scrub_returns_none_for_excluded_or_disabled():
    attrs = {"gen_ai.request.model": "gpt-4o"}
    assert scrub_attributes(attrs, LLMConfig(exclude_models=["gpt-4o"]), []) is None
    assert scrub_attributes(attrs, LLMConfig(disabled=True), []) is None


def test_otlp_exporter_requires_package(monkeypatch):
    import importlib.util as ilu
    real_find_spec = ilu.find_spec
    monkeypatch.setattr(
        ilu, "find_spec",
        lambda name, *a: None if "exporter" in name else real_find_spec(name, *a),
    )
    with pytest.raises(ImportError) as excinfo:
        _bridge.make_otlp_exporter(owner())
    assert "opentelemetry-exporter-otlp-proto-http" in str(excinfo.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest honeybadger/tests/contrib/test_llm.py -k scrub -v`
Expected: FAIL with `ImportError: cannot import name 'scrub_attributes'`

- [ ] **Step 3: Implement**

Append to `honeybadger/contrib/llm/_bridge.py`:

```python
_CONTENT_ATTRS = {
    "gen_ai.input.messages": "include_prompts",
    "gen_ai.system_instructions": "include_prompts",
    "gen_ai.output.messages": "include_responses",
}


def scrub_attributes(attributes, llm_config, params_filters):
    """Return a new, content-policied attributes dict for OTLP export,
    or None when the span must not be exported at all."""
    if llm_config.disabled:
        return None
    if _excluded(attributes.get("gen_ai.request.model"), llm_config.exclude_models):
        return None

    result = {}
    for key, value in attributes.items():
        flag = _CONTENT_ATTRS.get(key)
        if flag is None:
            result[key] = value
            continue
        if not getattr(llm_config, flag):
            continue  # drop content attribute entirely
        result[key] = _scrub_content_attr(
            value, params_filters, llm_config.max_content_length
        )
    return result


def _scrub_content_attr(raw, params_filters, max_content_length):
    import json as _json

    from ._policy import apply_content_policy

    try:
        decoded = _json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return "[unparseable content removed]"
    if not isinstance(decoded, list):
        decoded = [decoded]
    messages = [m if isinstance(m, dict) else {"content": m} for m in decoded]
    policied = apply_content_policy(messages, params_filters, max_content_length)
    return _json.dumps(policied, ensure_ascii=False, default=repr)


def make_otlp_exporter(owner):
    import importlib.util

    if importlib.util.find_spec("opentelemetry.exporter.otlp.proto.http") is None:
        raise ImportError(
            "export='otlp' requires opentelemetry-exporter-otlp-proto-http "
            "(not part of the honeybadger[llm] extra): "
            "pip install opentelemetry-exporter-otlp-proto-http"
        )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    config = honeybadger.config
    wrapped = OTLPSpanExporter(
        endpoint=config.endpoint.rstrip("/") + "/v1/traces",
        headers={"X-API-Key": config.api_key},
    )

    class ScrubbingOTLPExporter(SpanExporter):
        def export(self, spans):
            if not getattr(owner, "active", False):
                return SpanExportResult.SUCCESS
            llm_config = honeybadger.config.insights_config.llm
            filters = honeybadger.config.params_filters
            out = []
            for span in spans:
                scrubbed = scrub_attributes(
                    dict(span.attributes or {}), llm_config, filters
                )
                if scrubbed is None:
                    continue
                out.append(_clone_span(span, scrubbed))
            if not out:
                return SpanExportResult.SUCCESS
            return wrapped.export(out)

        def shutdown(self):
            wrapped.shutdown()

        def force_flush(self, timeout_millis=30000):
            return wrapped.force_flush(timeout_millis)

    def _clone_span(span, attributes):
        return ReadableSpan(
            name=span.name,
            context=span.get_span_context(),
            parent=span.parent,
            resource=span.resource,
            attributes=attributes,
            events=span.events,
            links=span.links,
            kind=span.kind,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    return ScrubbingOTLPExporter()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest honeybadger/tests/contrib/test_llm.py -v`
Expected: all PASS (the `make_otlp_exporter` ImportError test passes on rows without the exporter package; on rows with it installed the monkeypatched `find_spec` still forces the error path).

- [ ] **Step 5: Commit**

```bash
git add honeybadger/contrib/llm/_bridge.py honeybadger/tests/contrib/test_llm.py
git commit --no-gpg-sign -m "feat(llm): add scrubbed OTLP export escape hatch"
```

---

### Task 9: Packaging — extras, `python_requires`, mypy, dev deps

**Files:**
- Modify: `setup.py`
- Modify: `mypy.ini` (create the section if the file lacks it; the Oban branch adds this file — coordinate if both land)
- Modify: `dev-requirements.txt`

**Interfaces:**
- Produces: `pip install honeybadger[llm]` resolves on all interpreters (markers skip deps below 3.10); `honeybadger.contrib.llm` ships in the wheel.

- [ ] **Step 1: Update `setup.py`**

Replace the `packages`, add `python_requires` and `extras_require`, and refresh classifiers:

```python
    packages=["honeybadger", "honeybadger.contrib", "honeybadger.contrib.llm"],
    python_requires=">=3.9",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: System :: Monitoring",
    ],
    install_requires=["psutil", "six"],
    extras_require={
        "llm": [
            'opentelemetry-sdk>=1.43,<2; python_version >= "3.10"',
            'opentelemetry-instrumentation-genai-openai>=1.0b0,<1.1; python_version >= "3.10"',
        ],
    },
```

Before committing, verify the pins still resolve: `python3 -m pip index versions opentelemetry-instrumentation-genai-openai` (or check PyPI) — adjust the range only if 1.0b0/<1.1 is stale, and update the spec if so.

- [ ] **Step 2: Add mypy ignore**

In `mypy.ini` (append; create file with `[mypy]` header if absent on this branch):

```ini
[mypy-opentelemetry.*]
ignore_missing_imports = True
```

- [ ] **Step 3: Add dev/test deps**

Append to `dev-requirements.txt`:

```
opentelemetry-sdk>=1.43,<2; python_version >= "3.10"
opentelemetry-instrumentation-genai-openai>=1.0b0,<1.1; python_version >= "3.10"
openai>=1.26.0; python_version >= "3.10"
httpx; python_version >= "3.10"
```

- [ ] **Step 4: Verify**

Run: `pip install -e '.[llm]' && python -c "from honeybadger.contrib.llm import LLMHoneybadger; print('ok')"`
Expected: `ok` (on Python ≥3.10).
Run: `pytest honeybadger/tests` — all PASS.

- [ ] **Step 5: Commit**

```bash
git add setup.py mypy.ini dev-requirements.txt
git commit --no-gpg-sign -m "feat(llm): package [llm] extra with env markers and python_requires"
```

---

### Task 10: Integration tests (real instrumentor, mocked transport)

**Files:**
- Test: `honeybadger/tests/contrib/test_llm_integration.py`

**Interfaces:**
- Consumes: everything; this is the end-to-end proof and generates the observed rows for the attribute matrix in Task 11.

- [ ] **Step 1: Write the tests**

Create `honeybadger/tests/contrib/test_llm_integration.py`:

```python
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
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "hello there"},
        "finish_reason": "stop",
    }],
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
    client = openai_client(
        lambda request: httpx.Response(200, json=CHAT_RESPONSE)
    )
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
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
         "choices": [{"index": 0, "delta": {"content": "hel"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
         "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
         "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2,
                                  "total_tokens": 11}},
    ]
    body = "".join("data: %s\n\n" % json.dumps(chunk) for chunk in chunks) + "data: [DONE]\n\n"
    client = openai_client(
        lambda request: httpx.Response(
            200, content=body.encode(),
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


def test_early_terminated_stream_still_emits(llm):
    chunks = [
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
         "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]},
    ] * 5
    body = "".join("data: %s\n\n" % json.dumps(chunk) for chunk in chunks) + "data: [DONE]\n\n"
    client = openai_client(
        lambda request: httpx.Response(
            200, content=body.encode(),
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
```

- [ ] **Step 2: Run**

Run: `pytest honeybadger/tests/contrib/test_llm_integration.py -v`
Expected on Python ≥3.10 with the extra installed: all PASS. On other rows: SKIPPED. If an assertion fails because the instrumentor's actual attribute behavior differs from expectations, adjust the *test expectation* to observed reality, and record the discrepancy for Task 11's attribute matrix — the tests document observed behavior; the adapter must tolerate it either way.

- [ ] **Step 3: Commit**

```bash
git add honeybadger/tests/contrib/test_llm_integration.py
git commit --no-gpg-sign -m "test(llm): add end-to-end integration tests with mocked OpenAI transport"
```

---

### Task 11: Maintainer doc + README section

**Files:**
- Create: `honeybadger/contrib/llm.md`
- Modify: `README.md` (add "LLM Monitoring" section after the existing framework sections)

- [ ] **Step 1: Write `honeybadger/contrib/llm.md`**

Mirror the structure of the Oban branch's `honeybadger/contrib/oban.md`: What the contrib does / Integration surfaces / Lifecycle / Why the non-obvious choices / Configuration / Limitations. Must include, at minimum, these sections with real content (summarize from the spec, do not point at the spec for the substance):

- **What the contrib does** — spans from the official genai-openai instrumentor become `llm.chat`/`llm.embedding` Insights events; content opt-in.
- **The attribute matrix** — a table of schema field × source attribute × observed availability (sync / async / streaming / error), filled from Task 10's observed results. Any field the integration tests never observed is marked "unverified".
- **Why the non-obvious choices** — private TracerProvider; batch exporter off the hot path (Simple processor under Lambda); `honeybadger.context.*` snapshot (ContextVar doesn't cross the export thread); `provider_response_id` naming (event() merge order); env-gating rules and restore-on-tearDown; owned vs borrowed provider lifecycle; inert-after-teardown; the PyPI provenance trap.
- **Configuration** — the `LLMConfig` table (field / default / effect), matching Task 2 exactly.
- **Limitations** — no embedding input content; provider misattribution for OpenAI-compatible endpoints (use `host`); free-form prompt text cannot be key-filtered; streaming usage requires `include_usage`; Python ≥3.10 for the extra.

- [ ] **Step 2: Write the README section**

Add to `README.md` (leading with the opt-in snippet per spec):

````markdown
## LLM Monitoring (beta)

Honeybadger can automatically log your OpenAI calls — model, token usage,
duration, and errors — as Insights events. Prompts and responses are **off
by default** and can be enabled with one flag:

```python
# pip install 'honeybadger[llm]'  (Python 3.10+)
from honeybadger import honeybadger

honeybadger.configure(
    api_key="{{PROJECT_API_KEY}}",
    insights_enabled=True,
    insights_config={
        "llm": {
            "include_prompts": True,   # opt-in: logs prompt content
            "include_responses": True, # opt-in: logs response content
        }
    },
)
```

Django, Flask, and ASGI integrations activate LLM instrumentation
automatically when the extra is installed. Elsewhere, initialize explicitly:

```python
from honeybadger.contrib.llm import LLMHoneybadger

LLMHoneybadger().init()
```

Configuration options (under `insights_config["llm"]`): `disabled`,
`include_prompts`, `include_responses`, `max_content_length` (default 8192
chars per message), `max_event_bytes` (default 65536), and `exclude_models`
(exact strings or compiled regexes). Prompt/response content is filtered
with `params_filters` and truncated before it leaves your process.

Notes: streaming OpenAI calls report token usage only when you pass
`stream_options={"include_usage": True}`; embedding inputs are never
captured. Advanced: `LLMHoneybadger(export="otlp")` sends standard
OTel-shaped spans to Honeybadger's OpenTelemetry endpoint instead of
`llm.*` events (requires `opentelemetry-exporter-otlp-proto-http`).
````

- [ ] **Step 3: Commit**

```bash
git add honeybadger/contrib/llm.md README.md
git commit --no-gpg-sign -m "docs(llm): add maintainer doc and README section"
```

---

### Task 12: Example app

**Files:**
- Create: `examples/llm_app/app.py`
- Create: `examples/llm_app/README.md`
- Create: `examples/llm_app/requirements.txt`

- [ ] **Step 1: Write the example**

`examples/llm_app/requirements.txt`:

```
honeybadger[llm]
openai>=1.26.0
flask
```

`examples/llm_app/app.py` — a self-contained script that (a) starts a stub OpenAI-compatible server on a thread so no API key is needed, (b) configures Honeybadger from `HONEYBADGER_API_KEY`, (c) makes one non-streaming and one streaming chat call:

```python
"""End-to-end demo: emits llm.chat events to Honeybadger Insights.

Usage:
    HONEYBADGER_API_KEY=... python app.py
No OpenAI key needed — a local stub serves canned completions.
"""
import json
import os
import threading

from flask import Flask, request, jsonify

stub = Flask("openai-stub")


@stub.post("/chat/completions")
def chat():
    if request.json.get("stream"):
        def sse():
            for text in ("Hello", " from", " the stub"):
                yield "data: %s\n\n" % json.dumps({
                    "id": "c1", "object": "chat.completion.chunk", "created": 1,
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": {"content": text},
                                 "finish_reason": None}],
                })
            yield "data: %s\n\n" % json.dumps({
                "id": "c1", "object": "chat.completion.chunk", "created": 1,
                "model": "gpt-4o", "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3,
                          "total_tokens": 11},
            })
            yield "data: [DONE]\n\n"
        return stub.response_class(sse(), mimetype="text/event-stream")
    return jsonify({
        "id": "chatcmpl-demo", "object": "chat.completion", "created": 1,
        "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"role": "assistant",
                     "content": "Hello from the stub"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    })


def main():
    threading.Thread(
        target=lambda: stub.run(port=8899), daemon=True
    ).start()

    from honeybadger import honeybadger
    honeybadger.configure(
        api_key=os.environ["HONEYBADGER_API_KEY"],
        insights_enabled=True,
        force_report_data=True,
        insights_config={"llm": {"include_prompts": True,
                                 "include_responses": True}},
    )
    from honeybadger.contrib.llm import LLMHoneybadger
    llm = LLMHoneybadger(instruments=["openai"]).init()

    import openai
    client = openai.OpenAI(api_key="sk-stub", base_url="http://127.0.0.1:8899")

    client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "Say hello"}]
    )
    stream = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "Stream hello"}],
        stream=True, stream_options={"include_usage": True},
    )
    for _ in stream:
        pass

    llm.tearDown()  # flushes spans and the events worker
    print("Done — check Insights for llm.chat events.")


if __name__ == "__main__":
    main()
```

`examples/llm_app/README.md`: setup (venv, `pip install -r requirements.txt`), run command, what to expect in Insights, and a sample BadgerQL query (`filter event_type::str == "llm.chat" | fields ts, model, input_tokens, output_tokens, duration`).

- [ ] **Step 2: Verify manually**

Run: `HONEYBADGER_API_KEY=<a test project key from env> python examples/llm_app/app.py`
Expected: exits cleanly printing `Done`; two `llm.chat` events (one streaming) visible in the test project's Insights. Never hardcode the key.

- [ ] **Step 3: Commit**

```bash
git add examples/llm_app/
git commit --no-gpg-sign -m "docs(llm): add runnable example app with stub OpenAI server [skip ci]"
```

---

## Deferred / explicitly out of scope for this plan

- CI workflow matrix changes (adding a ≥3.10 row that installs the extra) — do together with the repo's workflow owner; integration tests already self-skip.
- Anthropic/Bedrock (phase 2), LangChain/agents + `llm.tool_call` (phase 3), dashboard template (phase 4).
- `llm.embedding` content capture (upstream gap), `report_exceptions` (spec: deferred).
- Packaging tests on a below-floor interpreter (requires multi-interpreter CI; covered by env markers + the `ImportError` unit test).
- Abandoned-stream (GC-finalized span) integration test — GC timing makes it inherently flaky; observed behavior documented in contrib/llm.md instead.

## Self-Review Notes

- Spec coverage: config (T2), filter (T1), adapter incl. error order/cache split/system-instructions (T3), content policy + budget (T4), bridge + context crossing + exclude/disabled gates + rate-limited warnings (T5), shell + env gating + ownership + Lambda sync path + auto_init (T6), framework wiring (T7), OTLP escape hatch (T8), packaging/markers/python_requires/mypy (T9), integration + streaming/early-break (T10), maintainer doc with attribute matrix + README (T11), example app (T12). Borrowed-provider inertness is covered by T5's `owner(active=False)` test plus T6 lifecycle; `remove_span_processor` non-existence is documented in T11.
- Types: `owner.active` (T5) = `LLMHoneybadger.active` (T6); `make_otlp_exporter(owner)` signature consistent in T6's `_build_exporter` and T8; `LLMConfig` field names match spec schema everywhere.
- Placeholders: none — every step carries runnable code or an exact command.
