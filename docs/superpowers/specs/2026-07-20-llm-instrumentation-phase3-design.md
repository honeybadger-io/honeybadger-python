# LLM Instrumentation Phase 3 (Frameworks) — Design

**Date:** 2026-07-20
**Status:** Draft — revised after Codex review (scope-based routing replaced with operation-name classification + response-id dedup; run-tree fields added; sampling fix; distribution-based detection); pending human review
**Parent spec:** `docs/superpowers/specs/2026-07-11-llm-instrumentation-design.md` (Phasing §3). This spec refines and amends it: three new event types, run-tree/correlation fields added to all `llm.*` span-derived events (including `llm.chat` — additive), a sampling-key mechanism in core, and resolution of phase 3's "evaluate a manual API" question (deferred).
**Base:** stacked on phase 2 (branch `llm-py-phase2-anthropic-bedrock`, PR #271). Assumes phase 1+2 as merged.

## Goal

Instrument LangChain/LangGraph and OpenAI Agents SDK applications so that a single agent run appears in Insights as a reconstructable tree of events:

- `llm.workflow` — one per run (graph/chain invocation, `Runner.run`/`run_sync`)
- `llm.agent` — one per agent invocation within a run
- `llm.tool_call` — one per tool/function call, with opt-in arguments/results
- the existing `llm.chat` events, joined to their run via `trace_id` and orderable/nestable via the new `span_id`/`parent_span_id`/span-start-`ts` fields

"Reconstructable tree" is a schema guarantee, not (yet) a UI: parentage and ordering are recoverable by query. A run-tree UI is phase-4 product work.

## Non-goals

- A manual instrumentation API (`@honeybadger.llm_tool` etc.) — **deferred until requested** (decision, 2026-07-20).
- The openllmetry-dialect adapter; revisiting Bedrock content capture; span-waterfall UI; evaluation/scoring (parent-spec non-goals stand).
- Analyzer detectors and dashboard treatment for the new event types — required for the product story but outside this repo; flagged as follow-ups.

## Background: what the instrumentors actually do (verified 2026-07-20)

Both are official contrib GenAI-family packages, 1.0b0, Python ≥3.10, same dependency series as our pins:

- **`opentelemetry-instrumentation-genai-langchain`** hooks LangChain's callback manager. Emits **workflow**, **agent**, and **tool** spans nested to mirror the graph, **and its own chat spans for LLM invocations** (`on_chat_model_start`/`on_llm_end` in the callback handler; 1.0b0 release notes: "Added span support for GenAI LangChain LLM invocation") including usage, response model, response ID, finish reason, and messages for supported models (`ChatOpenAI`, `ChatBedrock`). Since `langchain-openai` drives the `openai` SDK underneath, a model call made through LangChain produces **two chat spans** when our provider instrumentor is also active — dedup is a first-class requirement, not an edge case.
- **`opentelemetry-instrumentation-genai-openai-agents`** registers a tracing processor with the Agents runtime. Emits **workflow** (per `Runner.run`/`run_sync`, named from `RunConfig.workflow_name`), **agent**, and **tool** spans (function tool arguments and result). ⚠️ It leaves the Agents SDK's **built-in OpenAI trace exporter active by default** (opt-out: `disable_openai_trace_export=True` at instrument time) — a privacy-relevant default addressed in Components §6.
- **Critical constraint:** all GenAI-family instrumentors (providers and frameworks) create spans through the shared `opentelemetry-util-genai` `TelemetryHandler`, which acquires its tracer as `get_tracer(__name__)` — so **every span carries the same instrumentation scope** (`opentelemetry.util.genai.handler`). Scope cannot distinguish provider from framework spans, and any design assuming it can is unimplementable at this pin.

⚠️ Provenance trap (standing): unprefixed `opentelemetry-instrumentation-langchain` on PyPI is Traceloop's. Official = `opentelemetry-instrumentation-genai-*`.

## Architecture decision: operation-name classification + response-identity dedup

**Classification** uses the standardized `gen_ai.operation.name` values that util-genai stamps on every span — no scope needed:

| `gen_ai.operation.name` | Event |
|---|---|
| `chat` (and existing `embeddings`) | `llm.chat` / `llm.embedding` (deduped, below) |
| `invoke_workflow` | `llm.workflow` |
| `invoke_agent` | `llm.agent` |
| `execute_tool` | `llm.tool_call` |
| other `gen_ai.*` spans | `llm.call` (unchanged fallback) |

Exact operation-name strings are empirical checkpoint #1; the adapter treats them as data, not constants baked into multiple files.

**`framework` attribution** cannot come from scope. Sources, in order: a framework-identifying span attribute if one exists at this pin (checkpoint #2); else derivation from the operation (`invoke_workflow`/`invoke_agent`/`execute_tool` spans only exist when a framework instrumentor is active — when exactly one framework instrumentor is active on the instance, attribute to it; when both are active and no span attribute distinguishes them, emit the field only when unambiguous). The field is best-effort like every other; it must never be guessed wrong in preference to being omitted.

**Chat dedup: response-identity, not topology, not activation state.** Both chat spans for one model call describe the same provider response and carry the same `gen_ai.response.id`. The exporter keeps a small bounded LRU (e.g. 512 entries, keyed `(trace_id, provider_response_id)`) of recently emitted `llm.chat` events and **drops any chat span whose key was already emitted**. Properties:

- No duplicates in the overwhelmingly common case (both spans in one process, exported within the LRU window — they finish within milliseconds of each other).
- First-seen wins. Field fidelity may vary slightly by which span arrived first (the LangChain chat span carries usage at this pin, so neither ordering loses token counts); the maintainer doc records observed field variance per ordering.
- Chat spans with **no** `gen_ai.response.id` are emitted unconditionally (never suppressed on a guess). If observation (checkpoint #3) shows a systematic no-response-id duplicate pair, the fallback key is `(trace_id, parent_span_id-of-the-langchain-span == span_id-of-provider-span)` — only if the parent/child pair arrives in the same export batch; otherwise both are emitted and the limitation is documented.
- The LRU is per-exporter in-memory state — bounded, no cross-batch ordering assumption beyond the window, cleared on tearDown.

**Honest limits (documented, not hidden):** dedup is best-effort under adverse conditions — custom samplers on a borrowed provider, provider instrumentors skipped because the app pre-instrumented them, `exclude_models` discarding the provider event after its twin was suppressed (mitigation: the exclusion check runs BEFORE the dedup-LRU insert, so an excluded event never suppresses its twin), or LRU eviction under extreme concurrency. The failure mode in every residual case is a duplicate or a slightly-poorer single event — never silent data loss of a whole call.

**Rejected alternatives:** scope-based routing (impossible — shared scope, above); trace-topology dedup as the primary mechanism (cross-batch ordering makes it unreliable; retained only as the same-batch fallback); deactivating provider instrumentors when a framework is active (LangChain covers only "supported models" — non-LangChain calls in the same app would lose instrumentation entirely).

## Event schemas (amendment to the parent spec's frozen schema)

Shared conventions carry over (best-effort/omit-when-absent, content policy, `honeybadger.context.*` merge). Three additions apply to **all** span-derived `llm.*` events including existing `llm.chat`/`llm.embedding` (purely additive for phase-1 beta users):

| Field | Type | Notes |
|---|---|---|
| `span_id` | str (hex) | this span |
| `parent_span_id` | str (hex) | omitted for roots — this is what makes the tree reconstructable |
| `ts` | timestamp | now set from the **span start time** (event() honors a provided `ts`), so ordering reflects execution, not export batching |

**`llm.tool_call`** — `tool_name`, `tool_call_id` (from `gen_ai.tool.call.id` — connects the model's requested call to its execution), `tool_type` (from `gen_ai.tool.type`, when present), `framework`, `duration`, `trace_id`, `span_id`, `parent_span_id`, `conversation_id` (when the span carries it), `error`; `arguments` (opt-in, `include_prompts`) and `result` (opt-in, `include_responses`) — both **JSON values** (object/array/string as emitted), passed through the opaque-content policy (Components §4).

**`llm.agent`** — `agent_name`, `agent_id` (from `gen_ai.agent.id`, when present), `framework`, `duration`, `trace_id`, `span_id`, `parent_span_id`, `conversation_id` (LangChain emits it on agent invocations), `error`, `description` (static metadata, plain field).

**`llm.workflow`** — `workflow_name` (source: `gen_ai.workflow.name` if present; at 1.0b0 util-genai puts the name in the **span name** (`invoke_workflow {name}`), so the tested fallback is parsing it from the span name), `framework`, `duration`, `trace_id`, `span_id`, `parent_span_id`, `conversation_id` (when present), `error`; `input`/`output` (opt-in behind `include_prompts`/`include_responses`, opaque-content policy, JSON values).

**`llm.chat` (amended)** — gains `span_id`/`parent_span_id`/span-start `ts` (above) and `conversation_id` **when the chat span itself carries `gen_ai.conversation.id`**. Scope narrowed deliberately: OTel children inherit trace context, not parent attributes, and at this pin LangChain sets conversation/session IDs only on agent invocations — so `conversation_id` will usually appear on `llm.agent` events only. Cross-event conversation queries join via `trace_id` through the run's agent event. (A propagation mechanism — copying the ID to descendants — is possible future work if customers ask; not promised now.)

## Components (delta over phase 1+2)

1. **Registry + detection.** Keys `"langchain"` and `"openai_agents"`, auto-detected, pins `>=1.0b0,<1.1`. The registry tuple gains a 4th element, `dist_name` (optional): when set, detection verifies the installed **distribution** via `importlib.metadata.version(dist_name)` instead of `find_spec` on an import name. Required for `openai_agents` (the Agents SDK imports as the generic `agents` — a name any unrelated package could claim; distribution check: `openai-agents`) and used for `langchain` (checkpoint #7 settles the trigger: `langchain-core` distribution). Existing entries keep 3-tuples/`find_spec` (no behavior change).
2. **Core sampling fix (small, load-bearing).** `_should_sample_event` currently samples on `payload["request_id"]`, else a fresh random UUID per event — so one run's events sample independently and `events_sample_rate < 100` punches holes in run trees (root gone, children counted). Fix: `_should_sample_event` honors `payload["_hb"]["sampling_key"]` (highest precedence; `_hb` is already stripped before send), and the LLM bridge sets `sampling_key = trace_id` on every span-derived event. Whole runs are then sampled in or out atomically. Unit-tested in core's test file; benefits any future correlated-event producer.
3. **`_semconv.py`.** Interface stays `normalize(span) -> Optional[NormalizedLLMSpan]` (it still owns duration/trace/error/content extraction) with one added parameter: `normalize(span, dedup=None)` where `dedup` is the bridge-owned LRU checker — OR the dedup check stays entirely in `_bridge._export_one` after normalization (implementation's choice; the spec requirement is only that dedup happens in events mode, post-exclusion-check, pre-emit). `NormalizedLLMSpan` gains a `content: dict` field carrying the independently gated opaque values (`arguments`, `result`, `input`, `output`) so the bridge can gate each without schema-specific branches. New normalizers for the three operations; `span_id`/`parent_span_id`/start-`ts` extraction added for all span events.
4. **Opaque-content policy (`_policy.py`).** Tool arguments/results and workflow input/output are `any`-typed (util-genai JSON-serializes objects but passes plain strings through; OTel explicitly warns both may contain sensitive data). The message-list policy does not fit. New function `apply_opaque_content_policy(value, filter_keys, max_content_length)`: JSON-decode when the value is a JSON string; `filter_structure` for key redaction on any structure; truncate **every string leaf** (not just `content` keys) to `max_content_length`; return the (JSON-serializable) result. `enforce_event_budget` extends its drop order: when over `max_event_bytes`, drop in sequence `arguments` → `result` → `input` → `output` → then the existing prompts-oldest-first → response, setting `content_dropped` as today. Tests: plain strings, nested objects/arrays, filter keys at depth, Unicode, oversized values.
5. **`_bridge.py`.** Events mode: routing per the classification table; dedup LRU (post-exclusion, pre-emit); `sampling_key`; span-start `ts`. **OTLP mode is explicitly untouched by routing and dedup** — it stays a raw-span pipe (parent-spec contract): the existing GenAI gate already passes framework spans (they carry `gen_ai.*` attributes — checkpoint #4 confirms), and `_CONTENT_ATTRS` gains the tool/workflow content attribute names, scrubbed with the opaque-content policy (which handles the plain-string case correctly, unlike the current message-shaped `_scrub_content_attr` — that function routes to the opaque scrubber for these attrs).
6. **Agents SDK native trace exporter — explicit decision.** Default: **leave it active** (consistent with "never disturb another telemetry consumer"; disabling it would surprise users who rely on the OpenAI traces dashboard). Consequence documented prominently: with the Agents SDK, prompts/tool data continue flowing to OpenAI's own trace ingestion regardless of Honeybadger's content flags. Escape hatch: `LLMHoneybadger(instrument_options={"openai_agents": {"disable_openai_trace_export": True}})` — a new constructor arg, a dict of per-registry-key kwargs passed through to `instrumentor.instrument(**kwargs)`. Generic (any instrumentor option), no per-option API surface.
7. **Config.** No new `LLMConfig` fields (the two content flags gate everything; `exclude_models` inapplicable to framework events — documented; an `exclude_tools` knob waits for demand).
8. **Packaging.** Two extras lines (`>=1.0b0,<1.1`, markers ≥3.10); dev-requirements adds the instrumentors plus `langchain`/`langgraph`/`langchain-openai`/`openai-agents` test deps.
9. **Env gating.** Unchanged (`span_only` per phase 2); verify both new instrumentors honor it via the shared util-genai parse site (checkpoint #5).

## Testing

All framework tests gated on package availability; raw spans (RecordingProcessor) AND emitted events asserted throughout.

- **Unit (fake spans):** classification table incl. fallthrough; dedup — duplicate `(trace_id, response_id)` dropped, missing response-id emitted, exclusion-before-LRU ordering, LRU bound/eviction, tearDown clears; `span_id`/`parent_span_id`/start-`ts` extraction; opaque-content policy (full matrix from Components §4); budget drop order; `sampling_key` set; core `_hb.sampling_key` honored (in core's test file); OTLP scrub of tool/workflow content attrs incl. plain strings.
- **Integration — LangChain:** two-node LangGraph `StateGraph` with one tool over the mocked-transport OpenAI client. Assert: one `llm.workflow`; agent events as observed; ≥1 `llm.tool_call` with `tool_call_id`; `llm.chat` events sharing the workflow `trace_id`; **exactly one chat event per model call** (the dedup assertion — with both LangChain and provider instrumentors active); parentage chain reconstructable (`parent_span_id` links tool→agent→workflow as observed); both sync `invoke` and async `ainvoke`.
- **Integration — OpenAI Agents:** `Runner.run_sync` AND async `Runner.run` with a function tool over the mocked OpenAI transport; same assertions; plus two **concurrent** runs asserting trace separation (no cross-run parent links, no context bleed).
- **Adverse:** error inside a tool (error field on `llm.tool_call`, run still emits); cancellation mid-run (emit whatever spans completed).
- **Matrix:** per-framework rows in the maintainer doc; unobserved modes marked unverified.

## Rollout / product follow-ups (outside this repo)

- opticon analyzer: detectors for the new event types (verify the existing `llm` detector's `llm.*` match covers them).
- Phase-4 dashboard: run-tree panel consuming `span_id`/`parent_span_id`; per-run cost rollups via `stats sum(input_tokens) by trace_id`.
- Docs site: agent-observability page.

## Empirical checkpoints (resolve at implementation, record in maintainer doc)

1. Exact `gen_ai.operation.name` strings for workflow/agent/tool spans at 1.0b0.
2. Whether any span attribute identifies the emitting framework (for `framework` attribution).
3. Chat-span duplicate behavior: confirm both spans carry `gen_ai.response.id`; observed field variance by arrival order; whether any duplicate pair lacks response IDs.
4. Framework spans carry `gen_ai.*` attributes (OTLP gate interaction) — expected yes via util-genai.
5. Both instrumentors honor the phase-2 `span_only` content-env value.
6. Attribute names for workflow input/output, agent id/description, tool arguments/result, `gen_ai.conversation.id` placement; `workflow_name` span-name-parsing fallback verified.
7. Detection triggers: `openai-agents` and `langchain-core` distribution names via `importlib.metadata`.

## Resolved questions

1. **Event scope** — resolved (2026-07-20): full set — `llm.tool_call` + `llm.agent` + `llm.workflow`, plus run-tree fields on all span events. Rationale: run-level rollups and a reconstructable tree are the agent-observability story.
2. **Manual API** — resolved (2026-07-20): deferred until requested.
3. **Dedup strategy** — resolved (revised after Codex review, 2026-07-20): operation-name classification + response-identity LRU dedup with documented best-effort limits. Scope-based routing is unimplementable (shared util-genai tracer scope); activation-state dedup is unsound (provider-active ≠ provider-span-exists).
4. **Tool-content gating** — resolved: no new flags; arguments/input prompt-gated, results/output response-gated, via the new opaque-content policy.
5. **Agents SDK native trace exporter** — resolved: left active by default (don't disturb other consumers), consequence documented, generic `instrument_options` escape hatch.
6. **Run-integrity under sampling** — resolved: `_hb.sampling_key = trace_id` so runs sample atomically.
