# LLM Instrumentation Phase 2 (Anthropic + Bedrock) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend phase-1 LLM instrumentation to the Anthropic SDK and (contingent on empirical verification) AWS Bedrock, emitting the same `llm.*` Insights events.

**Architecture:** No new architecture — phase 2 plugs into phase 1's seams: new entries in the `_INSTRUMENTORS` registry (`honeybadger/contrib/llm/__init__.py`), adapter tolerance for provider quirks in `_semconv.py`, extras/dev-deps additions, and integration tests mirroring the OpenAI ones. Framework auto-init picks up new registry keys automatically.

**Tech Stack:** `opentelemetry-instrumentation-genai-anthropic>=1.0b0,<1.1` (Python ≥3.10, anthropic ≥0.51.0; same `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` gate with `SPAN_ONLY` mode); `opentelemetry-instrumentation-botocore==0.64b0` (the 0.65b0 release pins `opentelemetry-instrumentation==0.65b0`, which CONFLICTS with the genai family's `~=0.64b0` — the 0.64b0 pin is mandatory for coexistence).

**Base:** branch `llm-py-phase2-anthropic-bedrock`, stacked on `llm-py-instrumentation-plan` (PR #269). The phase-2 PR targets the phase-1 branch until #269 merges, then retargets master.

**Authoritative spec:** `docs/superpowers/specs/2026-07-11-llm-instrumentation-design.md` (Phasing §2). Where plan and spec disagree, the spec wins.

## Global Constraints

- All phase-1 Global Constraints still apply (lazy imports, omit-not-None fields, frozen schema names, env-gating rules, never override user-set env vars, full suite green before every commit, `feat(llm)`/`fix(llm)`/`test(llm)`/`docs(llm)` commit scopes with the Claude co-author trailer, `--no-gpg-sign`).
- ⚠️ Provenance trap: the unprefixed `opentelemetry-instrumentation-anthropic` on PyPI is Traceloop's. Phase 2 uses `opentelemetry-instrumentation-genai-anthropic` (official, verified publisher).
- Bedrock is **contingent**: Task 4 empirically verifies what `opentelemetry-instrumentation-botocore` 0.64b0 actually emits for Bedrock Runtime calls. If spans lack usable GenAI attributes (or content lives only on log events), Bedrock ships metadata-only or is deferred with the observed evidence recorded — the observed-reality rule from phase 1 applies: adapt to what the instrumentor does, never to what docs claim.
- The `llm.chat` schema is FROZEN by phase 1 (a customer is testing against it). Anthropic data maps into existing fields; new fields require spec amendment first.
- `.venv` already has the [llm] extra + openai; Task 1 adds anthropic/botocore packages to it.

## File Structure

```
honeybadger/contrib/llm/__init__.py   # + registry entries (anthropic, bedrock)
honeybadger/contrib/llm/_semconv.py   # + provider-quirk tolerance (only if Task 3/4 observation requires)
setup.py                              # + extras lines
dev-requirements.txt                  # + marker-gated test deps
honeybadger/tests/contrib/test_llm.py             # + registry/auto-detect unit tests
honeybadger/tests/contrib/test_llm_anthropic.py   # new: gated integration tests
honeybadger/tests/contrib/test_llm_bedrock.py     # new: gated integration tests (or deferral evidence)
honeybadger/contrib/llm.md            # + attribute matrix rows per provider
README.md                             # + provider list update
```

---

### Task 1: Registry + packaging for Anthropic and Bedrock

**Files:**
- Modify: `honeybadger/contrib/llm/__init__.py` (the `_INSTRUMENTORS` dict)
- Modify: `setup.py` (extras), `dev-requirements.txt`
- Test: `honeybadger/tests/contrib/test_llm.py` (append)

**Interfaces:**
- Produces: `_INSTRUMENTORS` gains `"anthropic"` and `"bedrock"` keys using the existing 3-tuple shape `(sdk_module_to_detect, instrumentor_module, instrumentor_class)`. Auto-detection (`instruments=None`) and explicit `instruments=[...]` work for the new keys with zero shell changes.

- [ ] **Step 1: Write the failing tests**

Append to the shell-test section of `honeybadger/tests/contrib/test_llm.py`:

```python
def test_registry_contains_phase2_providers():
    assert set(llm_module._INSTRUMENTORS) == {"openai", "anthropic", "bedrock"}
    for key, (sdk_mod, inst_mod, inst_cls) in llm_module._INSTRUMENTORS.items():
        assert isinstance(sdk_mod, str) and isinstance(inst_mod, str) and isinstance(inst_cls, str)


def test_unknown_instrument_error_lists_new_keys():
    with pytest.raises(ValueError) as excinfo:
        LLMHoneybadger(instruments=["watson"])._requested_instruments()
    assert "watson" in str(excinfo.value)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest honeybadger/tests/contrib/test_llm.py -k registry -v`
Expected: FAIL (`anthropic`/`bedrock` not in registry)

- [ ] **Step 3: Implement**

In `honeybadger/contrib/llm/__init__.py`, extend `_INSTRUMENTORS`:

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
    "bedrock": (
        "botocore",
        "opentelemetry.instrumentation.botocore",
        "BotocoreInstrumentor",
    ),
}
```

(Verify the two class/module names against the installed packages in Step 4 — if the import path differs, fix the tuple and note it in the report; the registry is the single source of truth.)

`setup.py` extras — append inside the existing `"llm"` list:

```python
        'opentelemetry-instrumentation-genai-anthropic>=1.0b0,<1.1; python_version >= "3.10"',
        # ==0.64b0 REQUIRED: 0.65b0 pins opentelemetry-instrumentation==0.65b0,
        # conflicting with the genai family's ~=0.64b0
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
Expected: `ok` (fix registry tuples if an import path differs). Resolver must NOT complain about `opentelemetry-instrumentation` version conflicts — if it does, STOP and report BLOCKED with the resolver output.

- [ ] **Step 5: Full suite + commit**

Run: `.venv/bin/pytest honeybadger/tests` — all green (the new packages must not disturb existing tests).

```bash
git add honeybadger/contrib/llm/__init__.py setup.py dev-requirements.txt honeybadger/tests/contrib/test_llm.py
git commit --no-gpg-sign -m "feat(llm): register anthropic and bedrock instrumentors"
```

---

### Task 2: Anthropic integration tests (mocked transport)

**Files:**
- Create: `honeybadger/tests/contrib/test_llm_anthropic.py`

**Interfaces:**
- Consumes: everything from phase 1 + Task 1's registry. Mirrors `test_llm_integration.py`'s structure (importorskip gating, `llm` fixture with `instruments=["anthropic"]`, mocked httpx transport — the anthropic SDK is httpx-based like openai's).
- The **observed-reality rule** applies: assertions encode expectations; adjust to observed instrumentor behavior and record every discrepancy in the report for Task 5's matrix. Do not modify `_semconv.py` in this task — if the adapter misses an attribute the instrumentor emits (fields missing from events), report DONE_WITH_CONCERNS naming the gap; Task 3 owns adapter changes.

- [ ] **Step 1: Write the tests**

Create `honeybadger/tests/contrib/test_llm_anthropic.py`:

```python
"""End-to-end: real genai-anthropic instrumentor + anthropic SDK against a
mocked HTTP transport, into a patched honeybadger.event. Skipped when the
packages aren't installed (Python < 3.10 rows)."""
import json
from unittest.mock import patch

import pytest

otel_anthropic = pytest.importorskip("opentelemetry.instrumentation.genai.anthropic")
anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx")

from honeybadger import honeybadger
from honeybadger.contrib.llm import LLMHoneybadger, CONTENT_ENV_VAR

MESSAGES_RESPONSE = {
    "id": "msg_test1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "hello there"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 9, "output_tokens": 3},
}


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv(CONTENT_ENV_VAR, "SPAN_ONLY")
    honeybadger.configure(
        api_key="fake",
        insights_enabled=True,
        insights_config={"llm": {"include_prompts": True, "include_responses": True}},
    )
    instance = LLMHoneybadger(instruments=["anthropic"])
    instance.init()
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
            model="claude-sonnet-4-5",
            max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        )
        flush(llm)
    assert mock_event.called
    event_type, data = mock_event.call_args[0]
    assert event_type == "llm.chat"
    assert data["provider"] == "anthropic"
    assert data["model"] == "claude-sonnet-4-5"
    assert data["input_tokens"] == 9
    assert data["output_tokens"] == 3
    assert data["duration"] >= 0


def test_error_response_end_to_end(llm):
    client = anthropic_client(
        lambda request: httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "slow down"}})
    )
    with patch.object(honeybadger, "event") as mock_event:
        with pytest.raises(anthropic.RateLimitError):
            client.messages.create(
                model="claude-sonnet-4-5", max_tokens=64,
                messages=[{"role": "user", "content": "hi"}],
            )
        flush(llm)
    assert mock_event.called
    data = mock_event.call_args[0][1]
    assert "error" in data and data["error"]


def test_content_captured_when_opted_in(llm):
    client = anthropic_client(lambda request: httpx.Response(200, json=MESSAGES_RESPONSE))
    with patch.object(honeybadger, "event") as mock_event:
        client.messages.create(
            model="claude-sonnet-4-5", max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        )
        flush(llm)
    data = mock_event.call_args[0][1]
    assert data.get("prompts"), "prompts expected with include_prompts=True"
    assert data.get("response"), "response expected with include_responses=True"


def test_streaming_end_to_end(llm):
    # Anthropic streams are SSE with typed events; build a minimal valid stream.
    events = [
        {"type": "message_start", "message": {**MESSAGES_RESPONSE, "content": [], "stop_reason": None, "usage": {"input_tokens": 9, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]
    body = "".join(
        "event: %s\ndata: %s\n\n" % (e["type"], json.dumps(e)) for e in events
    )
    client = anthropic_client(
        lambda request: httpx.Response(
            200, content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    with patch.object(honeybadger, "event") as mock_event:
        with client.messages.stream(
            model="claude-sonnet-4-5", max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            for _ in stream.text_stream:
                pass
        flush(llm)
    assert mock_event.called
    data = mock_event.call_args[0][1]
    assert data.get("model") == "claude-sonnet-4-5"
```

- [ ] **Step 2: Iterate to green, applying the observed-reality rule**

Run: `.venv/bin/pytest honeybadger/tests/contrib/test_llm_anthropic.py -v`
Adjust expectations to observed instrumentor behavior (e.g. `provider` value casing, `stop_reason`→`finish_reason` mapping, cache-token fields, whether streaming spans carry usage). Record EVERY observation in the report as attribute-matrix rows (sync / error / streaming) — Task 5 transcribes them. If events are missing schema fields the instrumentor does emit (adapter gap), note it for Task 3 and keep the test asserting observed current behavior.

- [ ] **Step 3: Full suite + commit**

```bash
git add honeybadger/tests/contrib/test_llm_anthropic.py
git commit --no-gpg-sign -m "test(llm): anthropic end-to-end integration tests with mocked transport"
```

---

### Task 3: Adapter quirks for Anthropic (only as observed)

**Files:**
- Modify: `honeybadger/contrib/llm/_semconv.py` (only if Task 2 found gaps)
- Test: `honeybadger/tests/contrib/test_llm_semconv.py` (append unit tests per change)

This task exists to absorb Task 2's DONE_WITH_CONCERNS findings. If Task 2 reported no adapter gaps, this task is a no-op: record "no changes needed" in the report and skip the commit. Known candidates to check (from Anthropic's API shape): `stop_reason` values arriving via `gen_ai.response.finish_reasons`; cache tokens via the already-mapped `gen_ai.usage.cache_read.input_tokens` / `cache_creation.input_tokens` (Anthropic is the main producer of these — verify they flow through to `cache_read_tokens`/`cache_creation_tokens`); any Anthropic-specific message-part types (`tool_use`, `thinking`) inside `gen_ai.output.messages` that `_flatten_parts` should treat as non-text parts rather than dropping the whole message.

Rules: every change gets a `ReadableSpan`-fake unit test with the OBSERVED attribute shape (copy real values from Task 2's report); every attribute stays optional; no new event fields without spec amendment.

- [ ] **Step 1: Enumerate gaps from Task 2's report** (skip task if none)
- [ ] **Step 2: TDD each gap** (failing fake-span test → adapter change → green)
- [ ] **Step 3: Full suite + commit**

```bash
git add honeybadger/contrib/llm/_semconv.py honeybadger/tests/contrib/test_llm_semconv.py
git commit --no-gpg-sign -m "fix(llm): adapter tolerance for observed anthropic attribute shapes"
```

---

### Task 4: Bedrock — empirical verification, then integrate or defer

**Files:**
- Create: `honeybadger/tests/contrib/test_llm_bedrock.py` (integration tests OR a documented deferral)
- Modify: `honeybadger/contrib/llm/_semconv.py` + `test_llm_semconv.py` (only if a dialect adapter tweak is needed)
- Possibly modify: `honeybadger/contrib/llm/__init__.py` (remove/keep the `bedrock` registry key per outcome), `setup.py`/`dev-requirements.txt` (remove botocore pin if deferring)

**The decision procedure (do this FIRST, before writing tests):**

1. Write a throwaway script (scratchpad, not committed): instrument botocore via `BotocoreInstrumentor().instrument(tracer_provider=<private provider with in-memory exporter>)`, then call `bedrock-runtime` `Converse` (and `InvokeModel`) against a `botocore.stub.Stubber`-stubbed client. Dump the resulting spans' name/kind/attributes/events.
2. Evaluate against three questions: (a) do spans carry GenAI attributes our adapter can classify (`gen_ai.operation.name`/`gen_ai.provider.name` or the legacy `gen_ai.system` dialect)? (b) where does message content live — span attributes (usable) or log events only (metadata-only for us)? (c) which token-usage attributes are present?
3. Outcomes:
   - **Usable spans, content on attributes** → full integration tests mirroring Task 2 (sync + error; streaming via `ConverseStream` if stubbable), adapter tweaks via the Task 3 pattern.
   - **Usable spans, content on log events only** → metadata-only Bedrock support: integration tests assert metadata fields and assert content is absent even with flags on; document in llm.md ("Bedrock content capture unavailable at this pin — instrumentor emits content as log events").
   - **No usable GenAI attributes** → defer Bedrock: remove the `bedrock` registry key and packaging lines from Task 1's changes, keep a `test_llm_bedrock.py` containing only a module docstring recording the observed evidence and a skip marker, and record the deferral in llm.md and the report.
4. Whatever the outcome, paste the observed span dump (trimmed) into the report — it is the evidence Task 5 documents.

- [ ] **Step 1: Run the decision procedure and record the outcome**
- [ ] **Step 2: Implement the matching outcome (tests/adapter/deferral) via TDD**
- [ ] **Step 3: Full suite + commit**

```bash
git add -A honeybadger/tests/contrib/test_llm_bedrock.py honeybadger/contrib/llm/ setup.py dev-requirements.txt
git commit --no-gpg-sign -m "feat(llm): bedrock instrumentation per observed botocore semconv"  # adjust message to outcome
```

---

### Task 5: Docs — matrix rows, README, maintainer notes

**Files:**
- Modify: `honeybadger/contrib/llm.md`, `README.md`

- [ ] **Step 1: Extend `honeybadger/contrib/llm.md`**: per-provider attribute-matrix sections (transcribe Task 2/3/4 observations faithfully, marking unobserved fields "unverified"); the botocore `==0.64b0` pin-conflict rationale (this is load-bearing packaging knowledge — future dependabot bumps will try to break it); Bedrock outcome (full/metadata-only/deferred) with evidence summary.
- [ ] **Step 2: Update `README.md`** LLM section: provider list ("OpenAI and Anthropic SDKs" + Bedrock per outcome), one Anthropic config example line, note that cache token fields (`cache_read_tokens`/`cache_creation_tokens`) are primarily populated by Anthropic.
- [ ] **Step 3: Full suite (docs-only, still verify) + commit**

```bash
git add honeybadger/contrib/llm.md README.md
git commit --no-gpg-sign -m "docs(llm): document anthropic/bedrock coverage and pin rationale"
```

---

## Deferred / explicitly out of scope for this plan

- LangChain / openai-agents instrumentors, `llm.tool_call` events, dedup rules (phase 3 per spec).
- The openllmetry-dialect adapter (spec: future work).
- Example-app Anthropic addition (the existing stub-server example demonstrates the pipeline; a second provider adds no new teaching value — YAGNI).
- Everything already deferred by phase 1 (CI workflow additions, GC-abandoned-stream test, embedding content, `report_exceptions`).

## Self-Review Notes

- Spec coverage (Phasing §2): official genai-anthropic ✓ (T1/T2/T3), Bedrock via contrib botocore ✓ contingent (T4, with the version-conflict discovery the spec couldn't know), provider keys to auto-detection ✓ (T1 — existing registry iteration), per-provider quirks to the adapter ✓ (T3/T4), extend attribute matrix ✓ (T5).
- Placeholder scan: Task 3 and 4 are deliberately outcome-conditional with explicit decision procedures rather than fabricated code for unverified upstream behavior — this is the phase-1 observed-reality rule applied at plan level, not a placeholder.
- Type consistency: registry tuple shape, fixture names, and schema field names match phase-1 code as merged.
