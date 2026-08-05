# LLM Instrumentation Phase 2 (Anthropic + Bedrock) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Revision note:** revised 2026-07-20 after a Codex plan review (12 findings incorporated — bedrock over-instrumentation containment, env-var casing resolution, raw-span observability, richer fixtures, executable Bedrock procedure, spec-amendment rule).

**Goal:** Extend phase-1 LLM instrumentation to the Anthropic SDK and (contingent on empirical verification) AWS Bedrock, emitting the same `llm.*` Insights events.

**Architecture:** Phase 2 plugs into phase 1's seams: new entries in the `_INSTRUMENTORS` registry (`honeybadger/contrib/llm/__init__.py`), adapter tolerance for provider quirks in `_semconv.py`, extras/dev-deps additions, and integration tests mirroring the OpenAI ones. Two shell changes ARE required (discovered in review): `_otel_available()` is currently OpenAI-specific and must become registry-driven, and Bedrock must be **explicit opt-in, never auto-detected** (see Global Constraints).

**Tech Stack:** `opentelemetry-instrumentation-genai-anthropic>=1.0b0,<1.1` (verified: imports as `opentelemetry.instrumentation.genai.anthropic.AnthropicInstrumentor`; Python ≥3.10; anthropic ≥0.51.0 via its `instruments` extra); `opentelemetry-instrumentation-botocore==0.64b0` (verified: imports as `opentelemetry.instrumentation.botocore.BotocoreInstrumentor`; the 0.65b0 release pins BOTH `opentelemetry-instrumentation==0.65b0` AND `opentelemetry-semantic-conventions==0.65b0`, each conflicting with the genai family's `~=0.64b0` — the 0.64b0 pin is mandatory for coexistence).

**Base:** branch `llm-py-phase2-anthropic-bedrock`, stacked on `llm-py-instrumentation-plan` (PR #269). The phase-2 PR targets the phase-1 branch until #269 merges, then retargets master.

**Authoritative spec:** `docs/superpowers/specs/2026-07-11-llm-instrumentation-design.md` (Phasing §2). Where plan and spec disagree, the spec wins. **Deferral rule:** the spec unconditionally scopes phase 2 as Anthropic + Bedrock. If Task 5's evidence justifies deferring Bedrock, phase 2 is NOT complete until the spec is amended — the deferring commit must also edit the spec's Phasing §2 (move Bedrock to phase 3 with a one-line evidence citation) so spec and reality never diverge silently.

## Global Constraints

- All phase-1 Global Constraints still apply (lazy imports, omit-not-None fields, frozen schema names, never override user-set env vars, full suite green before every commit, `feat(llm)`/`fix(llm)`/`test(llm)`/`docs(llm)` commit scopes with the Claude co-author trailer, `--no-gpg-sign`).
- ⚠️ Provenance trap: the unprefixed `opentelemetry-instrumentation-anthropic` on PyPI is Traceloop's. Phase 2 uses `opentelemetry-instrumentation-genai-anthropic` (official, verified publisher).
- ⚠️ **Bedrock containment:** `BotocoreInstrumentor` traces **every** botocore call (S3, DynamoDB, SQS, …), not just Bedrock. Two hard rules: (1) `"bedrock"` is **never auto-detected** — it activates only when explicitly requested via `instruments=["...", "bedrock"]` (the `botocore` module is ubiquitous; auto-detecting would silently instrument every AWS call in the process); (2) the OTLP exporter must gain a **GenAI classification gate** (drop spans with no `gen_ai.*` attribute) BEFORE bedrock can be activated in any environment — `export="otlp"` currently forwards every span on the provider, which with botocore instrumented means shipping S3/DynamoDB spans to the OTel endpoint. Tests must prove non-GenAI botocore spans are not exported.
- **Env-var casing:** phase 1 writes lowercase `span_only`; Anthropic's docs show uppercase `SPAN_ONLY`. Task 2 resolves this empirically and standardizes ONE value that both instrumentors accept, changing `_apply_env_gating` if needed. Until then, no fixture may hand-set the variable in a way that masks the production gating path.
- The `llm.chat` schema is FROZEN by phase 1 (a customer is testing against it). Anthropic data maps into existing fields; new fields require spec amendment first.
- Bedrock is **contingent** per Task 5's decision procedure and the Deferral rule above. The observed-reality rule from phase 1 applies everywhere: adapt to what instrumentors actually emit, never to what docs claim, and record observations as evidence.
- `.venv` already has the [llm] extra + openai; Task 1 adds anthropic/botocore/boto3 packages to it.

## File Structure

```
honeybadger/contrib/llm/__init__.py   # + registry entries; _otel_available() registry-driven; bedrock explicit-only
honeybadger/contrib/llm/_bridge.py    # + OTLP GenAI classification gate (Task 5)
honeybadger/contrib/llm/_semconv.py   # + provider-quirk tolerance (only as observed, Tasks 4/5)
setup.py                              # + extras lines
dev-requirements.txt                  # + marker-gated test deps
honeybadger/tests/contrib/test_llm.py             # + registry/availability/auto-detect unit tests
honeybadger/tests/contrib/llm_recording.py        # new: shared raw-span recording helper for integration tests
honeybadger/tests/contrib/test_llm_anthropic.py   # new: gated integration tests
honeybadger/tests/contrib/test_llm_bedrock.py     # new: gated integration tests (or executable module-level skip w/ evidence)
honeybadger/contrib/llm.md            # + attribute matrix rows per provider, pin rationale
README.md                             # + provider list update
docs/superpowers/specs/2026-07-11-llm-instrumentation-design.md  # ONLY if Bedrock defers (Deferral rule)
```

---

### Task 1: Registry, availability fix, and packaging

**Files:**
- Modify: `honeybadger/contrib/llm/__init__.py` (`_INSTRUMENTORS`, new `_EXPLICIT_ONLY`, `_otel_available()`, `_requested_instruments()`)
- Modify: `setup.py` (extras), `dev-requirements.txt`
- Test: `honeybadger/tests/contrib/test_llm.py` (append)

**Interfaces:**
- Produces: `_INSTRUMENTORS` gains `"anthropic"` and `"bedrock"` (existing 3-tuple shape). New module constant `_EXPLICIT_ONLY = frozenset({"bedrock"})`: `_requested_instruments()` with `instruments=None` returns all registry keys EXCEPT `_EXPLICIT_ONLY` members; explicit `instruments=[..., "bedrock"]` includes it. `_otel_available()` becomes registry-driven: True iff `opentelemetry.sdk` is importable AND at least one *requested* instrumentor's module is importable (signature becomes `_otel_available(requested=None)`; `None` keeps the old any-of-default behavior for `auto_init`). All checks keep the phase-1 `try/except ModuleNotFoundError` guard.

- [ ] **Step 1: Write the failing tests**

Append to the shell-test section of `honeybadger/tests/contrib/test_llm.py`:

```python
def test_registry_contains_phase2_providers():
    assert set(llm_module._INSTRUMENTORS) == {"openai", "anthropic", "bedrock"}
    for key, (sdk_mod, inst_mod, inst_cls) in llm_module._INSTRUMENTORS.items():
        assert isinstance(sdk_mod, str) and isinstance(inst_mod, str) and isinstance(inst_cls, str)


def test_bedrock_is_never_auto_detected():
    assert "bedrock" in llm_module._EXPLICIT_ONLY
    auto = LLMHoneybadger()._requested_instruments()
    assert "bedrock" not in auto
    assert "openai" in auto and "anthropic" in auto
    explicit = LLMHoneybadger(instruments=["bedrock"])._requested_instruments()
    assert explicit == ["bedrock"]


def test_unknown_instrument_raises():
    with pytest.raises(ValueError) as excinfo:
        LLMHoneybadger(instruments=["watson"])._requested_instruments()
    assert "watson" in str(excinfo.value)


def test_otel_available_is_registry_driven(monkeypatch):
    import importlib.util as ilu
    real_find_spec = ilu.find_spec

    def fake_find_spec(name, *args):
        if name == "opentelemetry.sdk":
            return object()
        if name == "opentelemetry.instrumentation.genai.anthropic":
            return object()
        if name.startswith("opentelemetry.instrumentation"):
            return None  # openai/botocore instrumentors absent
        return real_find_spec(name, *args)

    monkeypatch.setattr(ilu, "find_spec", fake_find_spec)
    # anthropic-only install: available for anthropic, not for openai-only request
    assert llm_module._otel_available(["anthropic"]) is True
    assert llm_module._otel_available(["openai"]) is False
    assert llm_module._otel_available() is True  # any-of default


def test_activation_uses_registry_tuples(monkeypatch):
    """instruments=['anthropic'] resolves module+class from the registry and
    calls instrument(tracer_provider=...) — mocked import machinery."""
    import importlib
    recorded = {}

    class FakeInstrumentor:
        is_instrumented_by_opentelemetry = False
        def instrument(self, tracer_provider=None):
            recorded["provider"] = tracer_provider

    fake_module = SimpleNamespace(AnthropicInstrumentor=FakeInstrumentor)
    monkeypatch.setattr(importlib, "import_module", lambda name: fake_module)
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: object()
    )
    instance = LLMHoneybadger(instruments=["anthropic"])
    provider = object()
    activated = llm_module._activate_instrumentors(instance, provider)
    assert activated == ["anthropic"]
    assert recorded["provider"] is provider
    assert "anthropic" in instance._instrumentors
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest honeybadger/tests/contrib/test_llm.py -k "registry or bedrock or otel_available or activation" -v`
Expected: FAIL (missing registry keys, missing `_EXPLICIT_ONLY`, old `_otel_available` signature)

- [ ] **Step 3: Implement**

`_INSTRUMENTORS` and `_EXPLICIT_ONLY` in `honeybadger/contrib/llm/__init__.py`:

```python
_INSTRUMENTORS = {
    "openai": (
        "openai",
        "opentelemetry.instrumentation.genai.openai",
        "OpenAIInstrumentor",
    ),
    "anthropic": (
        "anthropic",
        "opentelemetry.instrumentation.genai.anthropic",
        "AnthropicInstrumentor",
    ),
    # BotocoreInstrumentor traces EVERY botocore call (S3, DynamoDB, ...),
    # so bedrock is explicit-only: never part of auto-detection.
    "bedrock": (
        "botocore",
        "opentelemetry.instrumentation.botocore",
        "BotocoreInstrumentor",
    ),
}

_EXPLICIT_ONLY = frozenset({"bedrock"})
```

`_requested_instruments()`: default branch returns `[k for k in _INSTRUMENTORS if k not in _EXPLICIT_ONLY]`; explicit branch unchanged (validates against the full registry).

`_otel_available(requested=None)`:

```python
def _otel_available(requested=None):
    try:
        if importlib.util.find_spec("opentelemetry.sdk") is None:
            return False
        keys = requested if requested is not None else list(_INSTRUMENTORS)
        for key in keys:
            _sdk, module_name, _cls = _INSTRUMENTORS[key]
            if importlib.util.find_spec(module_name) is not None:
                return True
        return False
    except ModuleNotFoundError:
        return False
```

Update the two call sites: `init()` passes `self._requested_instruments()` (catch the ValueError from unknown keys before the availability check so the error stays a ValueError); `auto_init()` keeps the no-arg call.

`setup.py` extras — append inside the existing `"llm"` list:

```python
        'opentelemetry-instrumentation-genai-anthropic>=1.0b0,<1.1; python_version >= "3.10"',
        # ==0.64b0 REQUIRED: 0.65b0 pins opentelemetry-instrumentation==0.65b0
        # AND opentelemetry-semantic-conventions==0.65b0, both conflicting with
        # the genai family's ~=0.64b0
        'opentelemetry-instrumentation-botocore==0.64b0; python_version >= "3.10"',
```

`dev-requirements.txt` — append (marker-gated like the existing otel lines):

```
opentelemetry-instrumentation-genai-anthropic>=1.0b0,<1.1; python_version >= "3.10"
opentelemetry-instrumentation-botocore==0.64b0; python_version >= "3.10"
anthropic>=0.51.0; python_version >= "3.10"
boto3; python_version >= "3.10"
```

- [ ] **Step 4: Install and verify**

Run: `.venv/bin/pip install -e '.[llm]' 'anthropic>=0.51.0' boto3` then
`.venv/bin/python -c "from opentelemetry.instrumentation.genai.anthropic import AnthropicInstrumentor; from opentelemetry.instrumentation.botocore import BotocoreInstrumentor; print('ok')"`
Expected: `ok`. The resolver must NOT complain about `opentelemetry-instrumentation` or `opentelemetry-semantic-conventions` version conflicts — if it does, STOP and report BLOCKED with the resolver output.

- [ ] **Step 5: Full suite + commit**

Run: `.venv/bin/pytest honeybadger/tests` — all green.

```bash
git add honeybadger/contrib/llm/__init__.py setup.py dev-requirements.txt honeybadger/tests/contrib/test_llm.py
git commit --no-gpg-sign -m "feat(llm): register anthropic/bedrock, registry-driven availability, bedrock explicit-only"
```

---

### Task 2: Resolve content-capture env-var casing

**Files:**
- Possibly modify: `honeybadger/contrib/llm/__init__.py` (`_apply_env_gating`)
- Test: `honeybadger/tests/contrib/test_llm.py` and/or integration files

**The problem:** phase 1's `_apply_env_gating()` writes lowercase `span_only`. The OpenAI 1.0b0 docs show lowercase; the Anthropic 1.0b0 docs and the shared `opentelemetry-util-genai` docs show uppercase (`SPAN_ONLY`). If parsing is case-sensitive anywhere, our production gating silently captures nothing for one provider while hand-set-uppercase test fixtures pass.

- [ ] **Step 1: Establish ground truth from code, not docs.** Read the installed `opentelemetry-util-genai` source in `.venv` (`grep -rn "CAPTURE_MESSAGE_CONTENT" .venv/lib/python*/site-packages/opentelemetry/util/genai/ .venv/lib/python*/site-packages/opentelemetry/instrumentation/genai/`) and determine exactly how each instrumentor parses the variable (case-folded enum lookup? exact match?). Record the finding (file:line of the parse site) in the report.
- [ ] **Step 2: Standardize.** Pick the single value verified to enable span-content capture for BOTH instrumentors (prefer whatever the parse site canonicalizes to). If it differs from `span_only`, change `_apply_env_gating()` and the existing phase-1 tests asserting the value.
- [ ] **Step 3: Add the production-path test.** New integration test (in `test_llm_integration.py`, gated like its neighbors): with `CONTENT_ENV_VAR` UNSET (monkeypatch.delenv, raising=False), `include_prompts=True` config, real OpenAI instrumentor + mocked transport → assert prompts actually appear in the emitted event. This proves the value `_apply_env_gating` writes is one the instrumentor honors — the path every real customer exercises. (Task 3's Anthropic suite adds the equivalent test for Anthropic.)
- [ ] **Step 4: Full suite + commit**

```bash
git add honeybadger/contrib/llm/__init__.py honeybadger/tests/contrib/
git commit --no-gpg-sign -m "fix(llm): verify and standardize content-capture env value across instrumentors"
```

---

### Task 3: Anthropic integration tests (raw spans + events, mocked transport)

**Files:**
- Create: `honeybadger/tests/contrib/llm_recording.py` (shared helper)
- Create: `honeybadger/tests/contrib/test_llm_anthropic.py`

**Interfaces:**
- Consumes: phase 1 + Tasks 1-2. Mirrors `test_llm_integration.py`'s structure (importorskip gating, mocked httpx transport — the anthropic SDK is httpx-based).
- Produces: `llm_recording.RecordingProcessor` — a `SpanProcessor` that appends every ended span to a list, attached to the instance's provider alongside the normal pipeline. Integration tests assert BOTH the raw span attributes (what the instrumentor emitted) AND the emitted event (what our adapter produced). This is what makes adapter gaps *detectable*: a field absent from the event but present on the raw span is an adapter gap (→ Task 4); absent from both is an upstream omission (→ documented as unverified/absent).

```python
# honeybadger/tests/contrib/llm_recording.py
from opentelemetry.sdk.trace import SpanProcessor


class RecordingProcessor(SpanProcessor):
    """Collects ended spans so integration tests can assert raw attributes."""

    def __init__(self):
        self.spans = []

    def on_end(self, span):
        self.spans.append(span)
```

- **Observed-reality rule:** assertions encode expectations; adjust to observed instrumentor behavior, record every discrepancy AND every raw-span attribute dump (trimmed) in the report — Task 6 transcribes them. Do not modify `_semconv.py` here; report adapter gaps (raw-present/event-absent) as DONE_WITH_CONCERNS for Task 4.

- [ ] **Step 1: Write the tests**

Create `honeybadger/tests/contrib/test_llm_anthropic.py`. Skeleton below — note the richer fixtures (cache tokens, `stop_sequence`, mixed content parts) and the scenario list (sync, error, content-opt-in via the PRODUCTION gating path, streaming incl. early close and consumer exception, async):

```python
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
    assert data["input_tokens"] == 9
    assert data["output_tokens"] == 3
    assert data["duration"] >= 0
    # cache tokens: assert against raw span first, then event mapping
    raw = llm.recorder.spans[-1].attributes
    if "gen_ai.usage.cache_read.input_tokens" in raw:
        assert data["cache_read_tokens"] == 7
        assert data["cache_creation_tokens"] == 2
    # content via the production gating path
    assert data.get("prompts"), "prompts expected (production env-gating path)"
    assert data.get("response")


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
    assert mock_event.call_args[0][1].get("model") == "claude-sonnet-4-5"


def test_streaming_early_close_still_emits(llm):
    client = _sse_client(_stream_body())
    with patch.object(honeybadger, "event") as mock_event:
        with client.messages.stream(
            model="claude-sonnet-4-5", max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            next(iter(stream.text_stream), None)  # consume one chunk, then close
        flush(llm)
    assert mock_event.called


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
    assert mock_event.call_args[0][1]["provider"] == "anthropic"
```

- [ ] **Step 2: Iterate to green under the observed-reality rule.** Adjust expectations to observed behavior (provider casing, `stop_reason` mapping into `finish_reason`, whether the instrumentor emits cache-token attributes, streaming usage). For every adjustment record: raw span attributes (from `RecordingProcessor`) vs emitted event fields. A `tool_use`/`thinking` content-part scenario is welcome if the SDK stub cost is low; otherwise record those part types as **unverified** for Task 6. If the mocked-transport SSE stub fights the SDK accumulator, fix the stub against the *installed* SDK source, not by weakening assertions.
- [ ] **Step 3: Full suite + commit**

```bash
git add honeybadger/tests/contrib/llm_recording.py honeybadger/tests/contrib/test_llm_anthropic.py
git commit --no-gpg-sign -m "test(llm): anthropic end-to-end integration tests (raw spans + events)"
```

---

### Task 4: Adapter quirks for Anthropic (only as observed)

**Files:**
- Modify: `honeybadger/contrib/llm/_semconv.py` (only if Task 3 found raw-present/event-absent gaps)
- Test: `honeybadger/tests/contrib/test_llm_semconv.py` (append unit tests per change)

This task absorbs Task 3's DONE_WITH_CONCERNS findings — specifically fields present on RAW spans but missing from events (that's the definition of an adapter gap; upstream omissions are documented, not patched). Candidates: `stop_reason` values via `gen_ai.response.finish_reasons`; cache tokens via `gen_ai.usage.cache_read.input_tokens`/`cache_creation.input_tokens` (already mapped — verify flow-through); Anthropic-specific part types (`tool_use`, `thinking`) inside `gen_ai.output.messages` that `_flatten_parts` should treat as non-text parts.

Rules: every change gets a `ReadableSpan`-fake unit test using the OBSERVED attribute shape copied verbatim from Task 3's raw-span dumps; every attribute stays optional; no new event fields without spec amendment. If Task 3 reported no gaps, record "no changes needed" and skip the commit.

- [ ] **Step 1: Enumerate gaps from Task 3's report** (skip task if none)
- [ ] **Step 2: TDD each gap** (failing fake-span test with observed shape → adapter change → green)
- [ ] **Step 3: Full suite + commit**

```bash
git add honeybadger/contrib/llm/_semconv.py honeybadger/tests/contrib/test_llm_semconv.py
git commit --no-gpg-sign -m "fix(llm): adapter tolerance for observed anthropic attribute shapes"
```

---

### Task 5: OTLP GenAI gate, then Bedrock — verify, then integrate or defer

**Files:**
- Modify: `honeybadger/contrib/llm/_bridge.py` (GenAI classification gate in `ScrubbingOTLPExporter`) + tests in `test_llm.py`
- Create: `honeybadger/tests/contrib/test_llm_bedrock.py`
- Possibly modify: `_semconv.py` + `test_llm_semconv.py` (dialect tweak), `honeybadger/contrib/llm/__init__.py`, `setup.py`, `dev-requirements.txt` (on deferral), and the SPEC (Deferral rule)

- [ ] **Step 1 (unconditional, BEFORE any bedrock activation): OTLP GenAI gate.** In `ScrubbingOTLPExporter.export` (`_bridge.py`), drop any span whose attributes contain no key starting with `gen_ai.` — botocore instruments every AWS call, and OTLP mode must never ship S3/DynamoDB/SQS spans to the OTel endpoint. TDD: unit test with a fake span carrying only `rpc.system`/`aws.*` attributes → not exported; a genai span → exported. Note the behavior change in the `export="otlp"` docstring. Commit separately:

```bash
git add honeybadger/contrib/llm/_bridge.py honeybadger/tests/contrib/test_llm.py
git commit --no-gpg-sign -m "fix(llm): otlp exporter drops non-GenAI spans (bedrock containment)"
```

- [ ] **Step 2: Run the Bedrock decision procedure.** Throwaway script (scratchpad, NOT committed) — concrete skeleton, adjust as reality requires:

```python
# scratch: observe what botocore 0.64b0 emits for Bedrock Runtime
import json
import boto3
from botocore.stub import Stubber
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
# Logs/events signal: content may be emitted here, NOT on spans.
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor, InMemoryLogExporter
from opentelemetry import _logs as otel_logs
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

span_exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(span_exporter))

log_exporter = InMemoryLogExporter()
logger_provider = LoggerProvider()
logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
otel_logs.set_logger_provider(logger_provider)  # botocore's bedrock ext reads the global

BotocoreInstrumentor().instrument(tracer_provider=provider)
try:
    client = boto3.client(
        "bedrock-runtime", region_name="us-east-1",
        aws_access_key_id="testing", aws_secret_access_key="testing",
    )
    stubber = Stubber(client)
    stubber.add_response(
        "converse",  # snake_case boto3 method for the Converse API
        {
            "output": {"message": {"role": "assistant",
                                   "content": [{"text": "hello"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 9, "outputTokens": 3, "totalTokens": 12},
            "metrics": {"latencyMs": 5},
        },
    )
    with stubber:
        client.converse(
            modelId="anthropic.claude-3-5-sonnet-20241022-v2:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        )
    for span in span_exporter.get_finished_spans():
        print("SPAN", span.name, json.dumps(dict(span.attributes), default=str, indent=2))
        for event in span.events:
            print("  SPAN-EVENT", event.name, dict(event.attributes or {}))
    for record in log_exporter.get_finished_logs():
        print("LOG", record.log_record.attributes, str(record.log_record.body)[:300])
finally:
    BotocoreInstrumentor().uninstrument()
```

Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` (Task 2's standardized value) before instrumenting and repeat, to see whether/where content appears. Also probe `invoke_model` (body-based API) if `converse` yields usable spans. If the logs-SDK import paths differ at the installed version (`opentelemetry.sdk._logs` is provisional), adapt the script — the REQUIREMENT is that both the span signal AND the logs/events signal are captured in-memory; a span-only experiment cannot distinguish "content on logs" from "no content" and is an invalid basis for the decision.

- [ ] **Step 3: Decide and implement per outcome:**
  - **Usable spans, content on span attributes** → integration tests mirroring Task 3 (Stubber-based: sync `converse` + error via `stubber.add_client_error`; `converse_stream` if stubbable), adapter/dialect tweaks via the Task 4 pattern (botocore may emit the legacy `gen_ai.system` dialect — the adapter's `_SCALAR_FIELDS` already maps it; verify classification works since `gen_ai.operation.name` may be absent: extend `_OPERATION_EVENT_TYPES` handling per observed span names/attrs).
  - **Usable spans, content on log events only** → metadata-only Bedrock: integration tests assert metadata fields and assert content absent even with flags on; document in llm.md ("Bedrock content capture unavailable at this pin — instrumentor emits content on the logs signal, which the span bridge does not consume").
  - **No usable GenAI attributes** → defer Bedrock: revert the `bedrock` registry key and packaging lines, replace `test_llm_bedrock.py` with a module carrying the evidence docstring plus an executable `pytest.skip("bedrock deferred: <one-line reason>", allow_module_level=True)` so the skip is visible in test output, AND amend the spec per the Deferral rule (Phasing §2: move Bedrock to phase 3, cite the evidence).
- [ ] **Step 4: Whatever the outcome, paste the observed span/log dump (trimmed) into the report** — it is the evidence Task 6 documents.
- [ ] **Step 5: Full suite + commit**

```bash
git add -A honeybadger/tests/contrib/test_llm_bedrock.py honeybadger/contrib/llm/ setup.py dev-requirements.txt docs/superpowers/specs/
git commit --no-gpg-sign -m "feat(llm): bedrock instrumentation per observed botocore semconv"  # adjust message to outcome
```

---

### Task 6: Docs — matrix rows, README, maintainer notes

**Files:**
- Modify: `honeybadger/contrib/llm.md`, `README.md`

- [ ] **Step 1: Extend `honeybadger/contrib/llm.md`**: per-provider attribute-matrix sections transcribed faithfully from Task 3/4/5 raw-span evidence — every unobserved field/mode marked "unverified" (including tool_use/thinking parts and async streaming if Task 3 didn't cover them); the env-var casing resolution (Task 2's parse-site finding); the botocore `==0.64b0` pin-conflict rationale including the semantic-conventions conflict (load-bearing packaging knowledge — dependabot will try to break it); bedrock explicit-only rationale + the OTLP GenAI gate; the Bedrock outcome with evidence summary.
- [ ] **Step 2: Update `README.md`** LLM section: provider list per actual outcome; note bedrock requires explicit `instruments=["openai", "anthropic", "bedrock"]` (never auto-detected, and why); an Anthropic mention in the config example; cache-token fields note ONLY if Task 3 verified them flowing through (otherwise omit — no doc claims beyond evidence).
- [ ] **Step 3: Full suite (docs-only, still verify) + commit**

```bash
git add honeybadger/contrib/llm.md README.md
git commit --no-gpg-sign -m "docs(llm): document anthropic/bedrock coverage, casing, and pin rationale"
```

---

## Deferred / explicitly out of scope for this plan

- LangChain / openai-agents instrumentors, `llm.tool_call` events, dedup rules (phase 3 per spec).
- The openllmetry-dialect adapter (spec: future work).
- Example-app Anthropic addition (the existing stub-server example demonstrates the pipeline — YAGNI).
- `tool_use`/`thinking` content-part integration scenarios IF the SDK stub cost proves high (then marked unverified in the matrix, revisited in phase 3 alongside `llm.tool_call`).
- Everything already deferred by phase 1 (CI workflow additions, GC-abandoned-stream test, embedding content, `report_exceptions`).

## Self-Review Notes

- Spec coverage (Phasing §2): genai-anthropic ✓ (T1/T3/T4), Bedrock via contrib botocore ✓ contingent with an explicit spec-amendment rule on deferral (T5), provider keys to auto-detection ✓ with the deliberate, documented bedrock exception (T1 — auto-detecting the ubiquitous botocore module would instrument all AWS traffic; this is a plan-level deviation from the spec's "provider keys added to auto-detection" wording, justified in Global Constraints and to be reflected in llm.md), per-provider quirks ✓ (T4/T5), extend attribute matrix ✓ (T6).
- Codex review incorporation: bedrock containment (finding 1 → T1 `_EXPLICIT_ONLY` + T5 Step 1 OTLP gate), env casing (2 → T2), logs-signal observability (3 → T5 script), raw-span recorder (4 → T3 `RecordingProcessor`), richer fixtures (5 → cache tokens/system prompt in T3), SSE stub completeness (6 → `stop_sequence` + accumulator cross-check), spec-amendment rule (7 → header + T5), `_otel_available` fix (8 → T1), stronger registry tests (9 → T1), async/adverse streaming (10 → T3), executable Bedrock procedure (11 → T5 script skeleton), collected skip (12 → `allow_module_level=True`).
- Placeholder scan: Tasks 4 and 5 remain outcome-conditional by design with explicit decision procedures — the observed-reality rule at plan level, not placeholders.
