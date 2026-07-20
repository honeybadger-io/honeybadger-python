# LLM Instrumentation for honeybadger-python — Design

**Date:** 2026-07-11
**Status:** Draft — revised after Codex review and Kevin's review (2026-07-20: phase-1 engine switched from openllmetry to the official OTel instrumentation)
**Motivating request:** A Django customer asked for "the standard logging: LLM prompts, responses, and token usage." They evaluated PostHog, Pydantic (Logfire), and Raindrop for LLM observability but chose Honeybadger for speed and simplicity of error monitoring — they want LLM monitoring without adopting an all-in-one platform.

## Goal

Automatic instrumentation of LLM API calls made from Python applications, emitting Honeybadger Insights events that capture:

- Provider, model, and duration for every LLM call
- Token usage (input, output, cached) — the basis for cost tracking
- Errors (rate limits, timeouts, provider failures)
- Prompts and responses, **opt-in**, filtered and truncated

Setup must stay on-brand: one pip extra, a few lines of config, no collector or external telemetry pipeline to run.

## Non-goals

- Evaluation/scoring, feedback signals (thumbs up/down), or prompt management — product features, not SDK features. The event schema leaves room for them later.
- A full tracing UI with span waterfalls. We emit flat Insights events with correlation fields; Insights queries and dashboards are the UI.
- Instrumenting every LLM library on day one. Phased rollout (see Phasing).

## Background: prior art

- **honeybadger-ruby** ships a RubyLLM plugin that subscribes to `ActiveSupport::Notifications` and emits scalar metadata only (no prompts/responses). Python has no RubyLLM equivalent — no single unifying library with a built-in notification bus — so we must choose our own hook surface.
- **Raindrop's Python SDK** (customer's reference point) auto-instruments OpenAI/Anthropic/Bedrock by activating OpenTelemetry instrumentation packages under the hood and converting the resulting spans to its own events in-process. Users never see OTel. This validated the engine choice below.
- **The Oban contrib** (branch `oban-py-instrumentation-plan`, not yet on master) established the house pattern for a Python contrib: an `init()`/`tearDown()` shell with a single-instance guard, lazy imports, wrappers that go inert after teardown, a config dataclass under `insights_config`, deep-copy before filtering, a maintainer doc, an example app, and a thorough test file. This design reuses those conventions; where this doc says "Oban pattern," the reference is that branch's `honeybadger/contrib/oban.md`. (`tearDown()` keeps the camel-case name for consistency with that precedent.)

## Architecture decision: the engine

**Chosen: OpenTelemetry instrumentation packages as the capture engine, bridged to Insights events by a Honeybadger span exporter. No OTLP, no collector — spans are translated to `honeybadger.event()` calls in-process, off the request hot path.**

### How it works

1. User installs `honeybadger[llm]`, which pulls in the OTel SDK plus the instrumentation package(s) for supported providers.
2. `LLMHoneybadger().init()` creates a **private** `TracerProvider` (never installed as the global provider), attaches a `BatchSpanProcessor` wrapping a `HoneybadgerLLMSpanExporter`, and calls each detected instrumentor's `.instrument(tracer_provider=<private provider>)`.
3. The instrumentor patches the provider SDK (chat/completion calls, streaming iterators, async clients) and produces spans carrying LLM attributes.
4. The batch processor hands finished spans to our exporter **on its own background thread**, where the exporter normalizes attributes into the `llm.*` event schema, applies content policy (drop / filter / truncate / budget), and calls `honeybadger.event()`. The existing `EventsWorker` (bounded queue, drop-on-overflow, batching, retries) delivers to the API.

Using the batch processor + exporter rather than a synchronous `SpanProcessor.on_end` is deliberate: normalization, deep-copying, filtering, and truncation of potentially large prompt payloads must not add latency to the application thread that completed the LLM call. The cost is a small in-memory span buffer and the need to flush on `tearDown()`/shutdown.

### Why this over hand-rolled SDK patches

The OpenAI and Anthropic SDKs expose no telemetry hooks (unlike Oban, which gave us `telemetry` events and an extensions API). Hand-rolling means monkey-patching unstable internal call paths and owning the hardest edge cases forever — streaming (usage arrives on the final chunk, and for OpenAI only with `stream_options={"include_usage": True}`), async clients, retries, tool-call deltas. The instrumentation packages are exactly the "someone else maintains the ugly patching" layer.

Additional providers and frameworks in later phases attach to the same bridge, but are **not** free: each dialect needs its own normalization adapter (see `_semconv.py` below), and framework instrumentors (LangChain) wrapping already-instrumented provider SDKs can double-emit — phase 3 must define parent/child span selection and deduplication rules before shipping.

Costs accepted:

- **Optional dependency weight.** The OTel SDK arrives only via the `[llm]` extra; the core package stays zero-dependency. The contrib module imports cleanly when the extra isn't installed (lazy imports, Oban pattern); `init()` raises a clear `ImportError` naming the extra.
- **Attribute-dialect churn.** See "Instrumentor selection" below.
- **Process-global patches.** See "Ownership and lifecycle" below.

### Instrumentor selection: official OTel packages

**Phase 1 uses the official `opentelemetry-instrumentation-openai-v2` from opentelemetry-python-contrib** (OTel-governed, verified PyPI publisher), not the openllmetry (Traceloop) packages.

An earlier draft of this spec chose openllmetry for two reasons that have since eroded (verified 2026-07-20, prompted by Kevin's review):

1. *Content location.* The official package originally captured message content only as OTel log events, which would have forced the bridge to consume a second signal type. Current releases (2.4b0+) support span-attribute content capture: with `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`, the `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` env var accepts `span_only` / `event_only` / `span_and_event` — `span_only` puts input/output messages on span attributes where our exporter reads them.
2. *Coverage.* The contrib repo's GenAI directory now ships official instrumentations for anthropic, google-genai, vertexai, langchain, and openai-agents, so later phases no longer need Traceloop either.

With the technical reasons gone, governance decides it: the official packages track the conventions the ecosystem is converging on, under OpenTelemetry governance, while openllmetry emits a legacy dialect owned by a single vendor we'd rather not depend on — betting on it would mean a forced dialect migration later. Both packages are beta; the mitigations below apply regardless.

**Dialects.** `_semconv.py` holds all attribute knowledge as **explicitly versioned adapters** — each owning attribute names, JSON decoding (`gen_ai.input.messages`/`gen_ai.output.messages` are JSON-encoded strings), and message-part schemas — not a flat alias table. Every attribute is optional at parse time; events degrade to whatever metadata is present. Phase 1 ships the current-semconv adapter. An openllmetry-dialect adapter is future work if we ever want to accept spans from users' existing Traceloop setups via the borrowed-provider path.

The pinned instrumentor version determines which OpenAI endpoints are covered (Chat Completions and embeddings are the baseline; **streaming and Responses API support must be verified against the pinned version at implementation time and explicitly documented as in or out of scope** — neither is confirmed in the package docs as of this writing, and a material gap for the motivating customer would reopen the hand-rolled fallback for that path). The implementation produces a tested attribute matrix — which schema fields are available per endpoint × sync/async × streaming — and the README documents fields as best-effort.

### Content-capture gating

The official instrumentor gates content capture with two process-global env vars (`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` to enable the experimental conventions, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only` for span-attribute content), which cannot represent our two independent flags. Rule:

- If the user has explicitly set either variable, we never override it.
- Otherwise `init()` sets `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` and sets capture to `span_only` iff `include_prompts or include_responses` (unset otherwise, so content is never captured into spans that won't emit it).
- The exporter enforces the two flags independently regardless — prompt attributes are dropped unless `include_prompts`, completion attributes unless `include_responses`.

Documented consequences: the env gates are process-global, so another OTel GenAI consumer in the same process shares the capture setting; and the `gen_ai_latest_experimental` opt-in means attribute names can move between instrumentor minors — which is why the pin range is narrow and the adapter is versioned.

### Ownership and lifecycle

Instrumentor patches are **process-global**; a private `TracerProvider` controls where spans go, not who owns the patch.

- `init()` checks each instrumentor's `is_instrumented_by_opentelemetry` flag. Already instrumented (by the app's own OTel setup or another library) → skip that provider, log a warning, and record that we do **not** own it. Detection is best-effort; concurrent init from multiple libraries remains inherently unsafe and is documented as unsupported.
- `tearDown()` calls `uninstrument()` **only** on instrumentors this instance activated.
- **Owned provider** (default): created by us, shut down by us on `tearDown()` (which also flushes the batch processor).
- **Borrowed provider** (`init(tracer_provider=...)` escape hatch, for apps that want our events *and* their own exporters): we add our batch processor to it and never shut it down. OTel has no `remove_span_processor()`, so on `tearDown()` the processor stays attached but the exporter gates on `self._initialized` and drops everything (inert-after-teardown, Oban pattern). Documented: borrowing mutates the provider for its lifetime.

### Alternatives considered

- **Hand-rolled patches for `openai`/`anthropic`** (like `contrib/db.py` patches Django's cursor). Zero dependencies and fully self-contained, but we own streaming/async/retry edge cases for every provider forever, and coverage stays narrow. Rejected on maintenance math; revisit only if the OTel dependency proves problematic in practice.
- **Docs-only OTLP path** ("point your existing OTel GenAI spans at our OTLP endpoint"). Zero SDK work but poor fit for the customer profile — they chose Honeybadger to avoid heavyweight telemetry setup. Remains a complementary docs page for teams already invested in OTel.
- **Auto-instrument + stock OTLP export** — considered in depth below; deferred, but partially adopted as the `export="otlp"` escape hatch.

#### The OTLP-export alternative (deferred)

Instead of the in-process bridge, the SDK could still auto-load the instrumentors (keeping customer setup at zero) but attach a stock `OTLPSpanExporter` pointed at Honeybadger's existing OTel endpoint (`/v1/traces`, `X-API-Key` auth), which converts every span to an `otel.span` Insights event preserving raw attributes.

**What would be identical in both designs.** The bulk of this spec's complexity carries over unchanged: instrumentor loading, ownership/lifecycle rules, config, and — critically — the content policy. Prompt redaction **must** run client-side in either design; once data leaves the customer's process it's too late to honor `include_prompts=False`, `params_filters`, or truncation. OTLP export therefore still requires a scrubbing layer that rewrites span attributes before serialization, plus an `on_start` hook injecting `event_context` (request ID, user ID) into span attributes to preserve request↔LLM-call correlation.

**What OTLP export would delete.** The `_semconv.py` normalization adapters and the reuse of `EventsWorker` — replaced by the battle-tested OTLP exporter (its own batching/retry/compression).

**What it would cost.**

- *Query ergonomics.* Customers would query `data.attributes['gen_ai.usage.prompt_tokens']` on generic `otel.span` events — in whichever dialect the pinned instrumentor emits — instead of `input_tokens` on `llm.chat`. For a flagship feature, that is materially worse BadgerQL.
- *Dialect churn relocates rather than disappears.* Without SDK-side normalization, an instrumentor upgrade that renames attributes breaks every saved query and dashboard, retroactively splitting historical data. The bridge pins that churn inside `_semconv.py`, where one release fixes it.
- *Fixing the ergonomics properly means server-side GenAI mapping* — teaching the collector/opticon ingestion to recognize GenAI spans and map them into `llm.*` events. That is cross-repo, cross-team work.
- Smaller items: a new `opentelemetry-exporter-otlp-proto-http` dependency; a second delivery pipeline whose config/failure modes don't inherit `insights_enabled`, `events_sample_rate`, or `before_event`; and Lambda/short-lived-process flush semantics that `EventsWorker.force_sync` already handles.

**Why it could win long-term.** If the server-side GenAI mapping were built, it would be a one-time fix serving every language SDK *and* every OTel-native customer who never installs our SDK at all — someone running Pydantic AI or the OpenAI Agents SDK with standard OTel would get Honeybadger LLM dashboards just by pointing their exporter at us. Dialect churn would be fixed once, server-side, retroactively — consistent with how we already prefer server-side normalization (the opticon analyzer, server-side cost). The in-process bridge can never offer that.

**Decision.** There is no current appetite for server-side GenAI mapping, so the in-process bridge ships the customer value now, entirely within this repo. The choice does not foreclose the OTLP path: the instrumentor layer and content-policy code carry over unchanged, and the emit target is built as a pluggable seam (see `export` below) so the OTLP direction can be promoted later if the server-side work lands.

#### The `export="otlp"` escape hatch

`LLMHoneybadger(export="events")` is the default (the in-process bridge). `export="otlp"` swaps the batch processor's exporter for a stock `OTLPSpanExporter` targeting `{config.endpoint}/v1/traces` with `X-API-Key: {config.api_key}`, for customers who prefer standard OTel-shaped `otel.span` events over our `llm.*` schema (e.g. to match an existing OTel setup or to keep raw span fidelity). With the official instrumentor these are current-semconv spans — exactly what an OTel-native consumer expects.

- The content policy still applies: a scrubbing wrapper around the exporter rewrites span attributes (part-drop → redact → truncate) per `LLMConfig` before serialization, and `exclude_models`/`disabled` checks still gate export. `ReadableSpan`s are immutable at `on_end`, so scrubbing happens in the wrapper exporter, on copies, at export time — same background thread.
- Normalization is skipped; no `llm.*` events are emitted in this mode, and the stock LLM dashboard (phase 4) targets `llm.*`, so OTLP-mode customers query `otel.span` themselves. Documented trade-off.
- `opentelemetry-exporter-otlp-proto-http` is **not** part of the `[llm]` extra; `init(export="otlp")` raises `ImportError` naming the package if it's missing.

## Components

### 1. `honeybadger/contrib/llm.py` — the contrib shell

```python
from honeybadger.contrib.llm import LLMHoneybadger

llm = LLMHoneybadger()   # optionally: instruments=["openai"], tracer_provider=..., export="otlp"
llm.init()
# ... later, e.g. in tests:
llm.tearDown()
```

Follows the Oban contrib shape:

- `init()` / `tearDown()`, both idempotent; single-active-instance guard (`RuntimeError` on a second `init()` from another instance).
- `init()` runs under `try/except` with best-effort cleanup on partial failure, so a caller can fix the environment and retry.
- All `opentelemetry.*` and instrumentor imports are lazy — the module imports on any Python/without the extra; `init()` raises `ImportError: install honeybadger[llm]` when deps are missing.
- `instruments` constructor arg (list of provider keys, default: auto-detect installed SDKs).
- `export` constructor arg: `"events"` (default, in-process bridge to `llm.*` events) or `"otlp"` (scrubbed spans to our OTel endpoint — see the escape-hatch section). Auto-init always uses `"events"`.
- **Auto-init from framework integrations.** `DjangoHoneybadgerMiddleware`, `FlaskHoneybadger`, and the ASGI plugin call `LLMHoneybadger().init()` during their own setup when the `[llm]` extra's dependencies are importable and `insights_config.llm.disabled` is false — the same it-just-works behavior as the Django cursor patch. Auto-init goes through a module-level `auto_init()` helper that creates (or reuses) one shared instance, so two integrations initializing in the same process (e.g. Django + Celery worker startup) don't trip the single-instance guard. It is silent when deps are absent (no ImportError from merely not using the feature) and a no-op when a user has already explicitly initialized their own instance. Plain scripts and non-integrated apps call `init()` explicitly.
- Ownership/teardown semantics as specified above.
- **No `report_exceptions` knob in phase 1.** Span exception events do carry `exception.type`/`exception.message`/`exception.stacktrace`, but the stacktrace is a pre-rendered string, not live frames — auto-`notify` from spans would produce degraded notices, and provider exceptions already propagate to user code where the existing integrations (Django middleware, Celery, etc.) report them with full frames. The `llm.*` event carries an `error` field for Insights. Revisit if customers ask for LLM-specific error grouping.

### 2. `HoneybadgerLLMSpanExporter` — the bridge

A `SpanExporter` fed by a `BatchSpanProcessor` (background thread). For each span:

1. Classify by attributes → event type (`llm.chat`, `llm.embedding`, `llm.tool_call`; unrecognized LLM spans → `llm.call`). Non-LLM spans are ignored.
2. Normalize via the `_semconv.py` adapter for the active dialect.
3. Apply the content policy pipeline (normative order):
   a. **Drop non-text parts** — base64/binary/image content is replaced with a placeholder (`"[image omitted]"`) before any copying, so large blobs are never carried further.
   b. **Structural redaction** — deep-copy, then apply a **list-aware recursive key filter** honoring `params_filters`. The existing `utils.filter_dict` recurses into dicts only; a new helper (or an extension to it) must traverse lists of message dicts. Documented limitation: key-based filtering cannot redact secrets embedded in free-form prompt *text* — that risk is inherent to opting into content capture.
   c. **Per-string truncation** — each content string capped at `max_content_length` characters with a `"... [TRUNCATED]"` marker.
   d. **Event budget** — serialized event capped at `max_event_bytes` (UTF-8); messages are dropped oldest-first (keeping the system prompt and final response when possible) with a `content_dropped: true` flag, so one oversized event can never poison an `EventsWorker` batch.
4. Apply `exclude_models`: plain strings match the request model **exactly**; compiled regexes match via `.search()`. Absent model → not excluded.
5. Re-check `insights_config.llm.disabled` and `insights_enabled` at emit time, so flipping config stops events without teardown (Oban pattern).
6. `honeybadger.event(event_type, data)`. Ambient `event_context` (request ID, user ID — already set by the Django/ASGI/Celery integrations) merges in via the existing `event()` path, giving user/request correlation for free.

Failures inside the exporter must never propagate into the OTel pipeline, but must not be invisible either: normalization/serialization failures log a **rate-limited warning** (first occurrence per failure class at `warning`, repeats at `debug`), consistent with `event()`'s error-level logging of `before_event` failures.

### 3. `LLMConfig` in `insights_config`

```python
@dataclass
class LLMConfig:
    disabled: bool = False
    include_prompts: bool = False
    include_responses: bool = False
    max_content_length: int = 8192          # per content string, chars
    max_event_bytes: int = 65536            # serialized event budget, UTF-8 bytes
    exclude_models: List[Union[str, Pattern]] = field(default_factory=list)
```

Added to `InsightsConfig` as `llm: LLMConfig = field(default_factory=LLMConfig)`. The existing `set_config_from_dict` / `dataclass_from_dict` machinery makes it configurable as a plain dict, consistent with `db`/`celery`:

```python
honeybadger.configure(
    api_key="...",
    insights_enabled=True,
    insights_config={
        "llm": {
            "include_prompts": True,
            "include_responses": True,
        }
    },
)
```

`include_prompts`/`include_responses` **default off**: prompts are among the most sensitive data a customer holds, and Ruby's RubyLLM plugin ships metadata-only. Because the motivating customer explicitly wants content, the README section leads with the opt-in snippet.

### 4. Event schema

**Decision: universal `llm.*` event types and field names, shared across languages.** This is a deliberate exception to Honeybadger's per-language event philosophy. Per-language names work because they're the native vocabulary of each ecosystem (`sql.active_record` is Rails' own name); LLM calls have no native vocabulary in any language — developers think in provider concepts (model, tokens, prompts) that are identical everywhere, so the universal schema *is* what they expect. It also means a single detector rule in the opticon analyzer, one dashboard template, and unified queries for mixed-stack teams. Follow-up outside this repo: the analyzer gains an `llm` detector on `llm.*`; the Ruby gem's existing `chat.ruby_llm` keeps its own detector until Ruby migrates or dual-emits.

All fields except `provider` and `duration` are best-effort: presence depends on the pinned instrumentor, endpoint, and streaming mode, per the tested attribute matrix. Fields with no source in a given mode are omitted, never zero-filled.

**`llm.chat`** — one event per chat/completion call:

| Field | Type | Notes |
|---|---|---|
| `provider` | str | `"openai"`, `"anthropic"`, … |
| `model` | str | requested model |
| `response_model` | str | model the provider actually served, when reported |
| `duration` | int (ms) | span duration |
| `input_tokens` / `output_tokens` | int | from usage attributes, when reported |
| `cached_tokens` | int | when reported |
| `streaming` | bool | when determinable from span attributes |
| `temperature` | float | when set |
| `finish_reason` | str | when reported |
| `error` | str | exception type from the span's exception event, falling back to span status description |
| `prompts` | list | opt-in; `[{role, content}, …]`, post content-policy |
| `response` | list | opt-in; `[{role, content}, …]` completion message(s), post content-policy |
| `request_id` | str | provider request ID **if** the instrumentor records it (verify per pinned version) |
| `trace_id` | str | span's trace ID hex |
| `content_dropped` | bool | present and true when the event budget dropped messages |

`trace_id` groups calls **only when they already share an OTel trace** — e.g. under a framework instrumentor's parent span in phase 3. Independent SDK calls each start their own trace; for those, correlation comes from `event_context` (request ID), not `trace_id`. The schema makes no stronger promise.

**`llm.embedding`** — `provider`, `model`, `duration`, `input_tokens`, `error`; input content behind `include_prompts`.

**`llm.tool_call`** — reserved in the schema for framework instrumentors (LangChain agents) and a possible manual API; not emitted in phase 1.

### 5. Packaging

`setup.py` gains an extra whose dependencies carry **environment markers** matching the instrumentor's real floor (Python ≥3.10 as of the current `opentelemetry-instrumentation-openai-v2` releases — to be confirmed against the pins chosen at implementation time):

```python
extras_require={
    "llm": [
        'opentelemetry-sdk>=1.25,<2; python_version >= "3.10"',
        # narrow, CI-tested range — exact pin range chosen at implementation time:
        'opentelemetry-instrumentation-openai-v2>=2.4b0,<2.5; python_version >= "3.10"',
    ],
}
```

Semantics: `pip install honeybadger[llm]` succeeds everywhere; on interpreters below the floor the marker skips the deps and `LLMHoneybadger().init()` raises `ImportError` naming both the extra and the Python floor. The instrumentor pin range is **narrow** (single minor series) precisely because the package is beta and the experimental conventions move attribute names between minors; widening the range is a deliberate, tested act. Implementation should also add the long-missing `python_requires` to `setup.py` for the core package (separate housekeeping, flagged here because packaging tests will touch it). Packaging tests cover install + init on a supported and an unsupported interpreter.

## Data flow

```
openai SDK call
  → openai-v2 instrumentor (patches create/stream/async, times the call,
    collects usage + content into span attributes)
  → span ends on private TracerProvider
  → BatchSpanProcessor buffers; background thread calls
    HoneybadgerLLMSpanExporter.export
      classify → normalize (_semconv adapter) → content policy
      (drop parts → redact → truncate → byte budget)
      → exclude_models / disabled checks → honeybadger.event("llm.chat", data)
  → EventsWorker (bounded queue, drop-on-overflow) batches
  → Honeybadger events API → Insights
```

### Streaming semantics

- The instrumentor ends the span when the stream is fully consumed; `duration` then covers time-to-last-token.
- OpenAI Chat Completions streams report usage only with `stream_options={"include_usage": True}`; without it, token fields are absent (documented).
- **Incomplete streams** (consumer breaks early, raises mid-iteration, or abandons the iterator): behavior follows the pinned instrumentor — typically the span ends at generator close with whatever was accumulated, and an abandoned iterator may end the span only at GC. We emit whatever span arrives (partial output, missing usage) rather than suppressing it; integration tests must cover early `break`, an exception raised inside the consuming loop, and an unconsumed stream, and the maintainer doc records the observed behavior per pinned version.

## Error handling

- Provider call fails → instrumentor records an exception event on the span → event carries `error` (exception type; fallback to status description); the exception itself propagates to the app and existing error reporting.
- Bridge failure (unexpected attribute shapes, serialization) → caught in the exporter, rate-limited warning as specified above, span dropped.
- All bridge work happens on the batch processor's background thread; the application thread pays only the instrumentor's attribute-collection cost.
- `EventsWorker.push()` drops on a full queue (no producer backpressure); an LLM event lost this way is logged by the existing worker paths.

## Testing

- **Bridge unit tests** (bulk of coverage, run on all CI rows): construct `ReadableSpan`-shaped fakes with current-semconv attributes (including JSON-encoded message attributes) and assert on the emitted event dicts — content policy order (part-drop → redact → truncate → budget), list-aware filtering (and non-mutation of inputs), budget-drop behavior and `content_dropped`, exclude_models semantics (exact string / regex `.search()` / absent model), disabled-at-emit-time, teardown inertness on owned and borrowed providers, malformed-attribute tolerance, multi-byte Unicode around both the char truncation and byte budget boundaries.
- **Integration tests** (rows where the extra installs): real `opentelemetry-instrumentation-openai-v2` instrumentor + `openai` SDK against a mocked HTTP transport (respx/httpx mock); sync, async, streaming (with/without `include_usage`), early-terminated and failing streams, and error responses, end-to-end into a patched `honeybadger.event`. These tests generate the attribute matrix documented in the maintainer doc.
- **OTLP-mode tests**: the scrubbing wrapper applies the full content policy and `exclude_models`/`disabled` gating to exported spans (asserted against an in-memory OTLP stand-in), original spans are never mutated, and `init(export="otlp")` without the exporter package raises the documented `ImportError`.
- **Config tests**: `LLMConfig` hydration through `insights_config` dicts, matching `test_config.py` conventions.
- **Packaging tests**: extra install + `init()` on supported and below-floor interpreters.
- **Example app** under `examples/llm_app/` (small Django or plain script hitting a stub server) for manual end-to-end verification, mirroring `examples/oban_app/`.

## Phasing

1. **Phase 1 — OpenAI.** `LLMHoneybadger`, exporter bridge, `_semconv.py` (current-semconv adapter), `LLMConfig`, `[llm]` extra, `contrib/llm.md` maintainer doc (including the attribute matrix and observed streaming semantics), README section, example app. Answers the motivating customer (Django + the dominant SDK) for the endpoints the pinned instrumentor covers.
2. **Phase 2 — Anthropic + Bedrock.** Official `opentelemetry-instrumentation-anthropic` (contrib GenAI directory) plus Bedrock via the contrib botocore instrumentation; provider keys added to auto-detection, per-provider quirks to the adapter; extend the attribute matrix.
3. **Phase 3 — Frameworks.** Official langchain/openai-agents instrumentors from contrib; `llm.tool_call` events; **dedup/parent-selection rules for framework spans wrapping already-instrumented provider calls** (a hard prerequisite — without it one logical call emits two events); evaluate a small manual API (`@honeybadger.llm_tool`) for custom agent loops; optionally an openllmetry-dialect adapter for borrowed-provider users on existing Traceloop setups.
4. **Phase 4 — Product surface.** Stock "LLM overview" Insights dashboard template (cost by model, latency, error rate, token trends); docs page for the raw-OTLP path for teams with existing OTel pipelines.

## Resolved questions

1. **Cross-language schema alignment** — resolved: universal `llm.*` schema, as an explicit exception to the per-language event philosophy (rationale in "Event schema"). Analyzer detector and Ruby-gem migration are follow-ups outside this repo.
2. **Auto-init in framework integrations** — resolved: yes; Django/Flask/ASGI integrations auto-init, matching how the Django integration already auto-patches the DB cursor (details in "Components §1").
3. **In-process bridge vs. OTLP export** — resolved: in-process bridge as the default engine; no server-side GenAI mapping for now (no current appetite for the collector-side work). Full analysis under "The OTLP-export alternative"; `export="otlp"` ships as an escape hatch, and the emit-target seam keeps the OTLP direction open.
4. **Official OTel instrumentation vs. openllmetry (Traceloop)** — resolved (Kevin's review, 2026-07-20): official `opentelemetry-instrumentation-openai-v2`. The original technical reasons for openllmetry (span-attribute content, coverage) no longer hold — the official package now supports `span_only` content capture and the contrib repo covers anthropic/langchain/etc. — and OTel governance beats a single-vendor dependency emitting a legacy dialect. Rationale and verification caveats in "Instrumentor selection."
5. **Cost estimation** — resolved: server-side only. The SDK sends token counts and model name; no `cost` field and no price table in the SDK. Dashboard queries use parameterized token prices so the customer supplies their rates and the query does the math — no maintained price table anywhere. Shapes the phase-4 dashboard template; no impact on SDK phases 1–3.
