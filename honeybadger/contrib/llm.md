# LLM contrib — maintainer notes

This document describes what `honeybadger/contrib/llm/` does and the *why* behind its non-obvious choices. It's for someone maintaining or extending the contrib. End-user usage and configuration live in the project [README](../../README.md) under "LLM Monitoring (beta)".

## What the contrib does

Phase 1 wires Honeybadger into OpenAI calls by delegating instrumentation to the official OpenTelemetry GenAI instrumentor and bridging its spans into Honeybadger:

- `opentelemetry-instrumentation-genai-openai` patches the `openai` SDK's chat/completions (and, per that package, embeddings) calls and emits OTel spans carrying `gen_ai.*` attributes on a private `TracerProvider` we own.
- A companion `SpanExporter` (`_bridge.py`) normalizes each span (`_semconv.py`) into a Honeybadger Insights event — `llm.chat` or `llm.embedding` — via `honeybadger.event(event_type, data)`.
- Prompt and completion content is **off by default**. Setting `include_prompts` and/or `include_responses` on `LLMConfig` both (a) tells the instrumentor to put message content on span attributes (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only`) and (b) tells the exporter to actually copy that content into the event, subject to the content policy (redaction, truncation, budget).
- An `export="otlp"` escape hatch swaps the in-process bridge for a scrubbing `OTLPSpanExporter` that ships standard OTel-shaped spans straight to Honeybadger's `/v1/traces` endpoint, for teams that would rather consume `otel.span` events than the `llm.*` schema.

## Integration surfaces

Three files do the work:

1. **`__init__.py`** — the `LLMHoneybadger` contrib shell: `init()`/`tearDown()`, instrumentor activation/deactivation, env-var gating for content capture, exporter selection, and the module-level `auto_init()` used by framework integrations.
2. **`_bridge.py`** — the span → event bridge. `export_spans`/`_export_one` is pure Python (no otel imports) so it unit-tests against duck-typed spans; `make_context_processor`, `make_events_exporter`, and `make_otlp_exporter` build the real otel `SpanProcessor`/`SpanExporter` subclasses, importing `opentelemetry` lazily inside their function bodies.
3. **`_semconv.py`** — the versioned attribute adapter (`ADAPTER_VERSION = "genai-1.0"`). All knowledge of `gen_ai.*` attribute names, JSON message decoding, and error extraction order lives here, isolated from the bridge and the contrib shell.

`honeybadger/contrib/django.py`, `flask.py`, and `asgi.py` each call `from honeybadger.contrib.llm import auto_init` during their own setup and invoke it — the same auto-instrumentation pattern used for the Django DB cursor patch.

## Lifecycle

`LLMHoneybadger(instruments=None, tracer_provider=None, export="events").init()` wires everything; `tearDown()` reverses it. Both are idempotent (`self._initialized` guards re-entry).

Only one `LLMHoneybadger` instance may be active per process at a time, enforced by a module-level `_active_instance` under `_lock` (a `threading.Lock`). A second `init()` from a different instance raises `RuntimeError("another LLMHoneybadger instance is active; tearDown() it first")`; a second `init()` on the exact same instance from a different thread while the first is mid-init raises `RuntimeError("init already in progress")`. `init()` sets `_active_instance` inside the lock *before* doing any real work, so this guard is race-free — a second thread can't slip past the check and start its own instrumentor activation concurrently.

`init()` runs the substantive work (env gating, provider construction, pipeline attach, instrumentor activation) under a `try/except`; any failure calls `_cleanup_wiring()` (best-effort: deactivate whatever instrumentors got activated, restore the env var, shut down an owned provider) and clears `_active_instance` before re-raising, so the caller can fix the underlying issue (e.g. install the `[llm]` extra) and retry.

`auto_init()` (module-level, called by the framework integrations) is separately serialized by its own `_auto_lock`, distinct from `LLMHoneybadger._lock`. It:
- Returns `None` silently if otel/the instrumentor aren't importable, `insights_enabled` is `False`, or `insights_config.llm.disabled` is `True` — never raises.
- Reuses a single shared `_auto_instance` across repeated calls (e.g. Django middleware setup *and* a Celery worker's Django app-ready hook in the same process) instead of tripping the single-instance guard.
- Defers to a user's own explicit `LLMHoneybadger().init()` — if `_active_instance` is already set to something other than `_auto_instance`, `auto_init()` returns `None` rather than fighting over ownership.

Ordering matters: a user-configured instance (`export="otlp"`, a custom `tracer_provider=`, etc.) must call `.init()` before the framework integration constructs and calls `auto_init()` — e.g. in the Django settings module or `wsgi.py`, not `AppConfig.ready()` (too late, middleware setup has already run `auto_init()` by then). Otherwise `auto_init()` claims the single `_active_instance` slot first and the user's later explicit `.init()` raises `RuntimeError("another LLMHoneybadger instance is active; tearDown() it first")`.

## Why the non-obvious choices

### Private `TracerProvider`

Instrumentor patches are process-global (the instrumentor monkeypatches `openai` SDK methods), but a private `TracerProvider` controls *where the resulting spans go* independent of that. `_build_provider()` creates a fresh `TracerProvider()` for an owned instance so our export pipeline never depends on — or interferes with — an application's own OTel setup. The `tracer_provider=` constructor arg is the escape hatch for apps that want to add our processor to their own provider instead (see "owned vs. borrowed" below).

### Batch exporter off the hot path; `SimpleSpanProcessor` under Lambda

`_attach_pipeline` normally installs a `BatchSpanProcessor`, so span normalization, content policy, and the Honeybadger event push all happen on the batch processor's background thread — the application thread calling the OpenAI SDK only pays the instrumentor's attribute-collection cost, not ours.

Under `honeybadger.config.is_aws_lambda_environment`, `_attach_pipeline` uses `SimpleSpanProcessor(exporter)` instead — synchronous, on the calling thread. A frozen Lambda execution environment between invocations can suspend the batch thread mid-flush and silently lose buffered spans; going synchronous accepts the small per-call latency cost in exchange for not losing data, mirroring the intent of the SDK's existing `force_sync` default for Lambda notices (though `force_sync` itself is a separate knob that only affects notice delivery, not events).

`SimpleSpanProcessor` only makes span *export* synchronous — the exporter's `honeybadger.event(...)` call still enqueues onto the async `EventsWorker` (`core.py` / `events_worker.py`), same as every other event source. So event *delivery* to Honeybadger remains asynchronous even under the Lambda path, and a hard freeze immediately after handler return (before the `EventsWorker`'s background thread sends the queued event) can still lose it. This is a pre-existing gap in the design spec, not something the synchronous span path claims to close on its own.

### `honeybadger.context.*` span-attribute snapshot

`honeybadger.event_context()` lives in a `ContextVar` scoped to the calling thread. The batch processor's export thread runs later, disconnected from that thread — reading `_get_event_context()` at export time would see an empty context. `make_context_processor()` installs a `SpanProcessor` whose `on_start` calls `snapshot_context_attributes(span)` *while still on the calling thread*, copying scalar (`str`/`int`/`float`/`bool`) context values onto the span as `honeybadger.context.<key>` attributes. `_export_one` later reads them back off the span (`span.attributes`) and merges them into the event data with `setdefault` — span-derived fields win on key collision, context fields fill in the rest. The same processor is attached regardless of `export` mode, so `export="otlp"` spans carry the same `honeybadger.context.*` attributes for OTLP-side correlation.

### `provider_response_id` naming, not `request_id`

`honeybadger.event(event_type, data)` builds `final_payload = {**self._get_event_context(), **payload}` in `core.py` — event-context keys are merged first (lower precedence), explicit payload keys win on collision. The response-object ID from `gen_ai.response.id` is deliberately named `provider_response_id` rather than `request_id`: `request_id` is Honeybadger's own request-correlation field, populated from event context, and if the span-derived data used that same name it would silently clobber the correlation ID it's supposed to sit alongside, rather than simply refusing to overwrite it. A distinctive name sidesteps the collision entirely instead of relying on merge-order behavior to save it.

### Env-gating rules and restore-on-`tearDown`

The instrumentor gates message-content capture with one process-global env var, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`, read once at instrument time — it can't represent our two independent `include_prompts`/`include_responses` flags. `_apply_env_gating()` (called before `_activate_instrumentors`, since the instrumentor reads the gate at instrument time):
- Never overrides a value the user has already set (checks `CONTENT_ENV_VAR in os.environ` first and returns immediately if present).
- Otherwise sets it to `"span_only"` iff `include_prompts or include_responses`, and records `self._env_was_set_by_us = True` so `tearDown()` knows it owns the value.

`_restore_env_gating()` (called from `_cleanup_wiring()`, so it runs both on a clean `tearDown()` and on a failed `init()`) pops the env var **only if `self._env_was_set_by_us` is true** — if the user set it themselves, or if we never touched it (content flags were off), teardown leaves it exactly as found. The exporter also independently enforces `include_prompts`/`include_responses` per-event regardless of the env var's state, since the env var is a blunt, process-wide switch and the two flags are the fine-grained ones.

### Content-capture env-var casing is a non-issue — except for Bedrock

`_apply_env_gating()` writes `"span_only"` (lowercase) into `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`. This is safe for both the openai and anthropic instrumentors: the single parse site for that env var, `get_content_capturing_mode()` in `opentelemetry/util/genai/utils.py:20-28`, calls `.upper()` on the value before an `Enum[]` lookup against `ContentCapturingMode` (members `NO_CONTENT`, `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT`, in `opentelemetry/util/genai/types.py:19-27`) — so `"span_only"` and `"SPAN_ONLY"` are equivalent by construction. Both `opentelemetry-instrumentation-genai-openai` and `-genai-anthropic` route through the same shared `TelemetryHandler.__init__` (`opentelemetry/util/genai/handler.py:102-109`) to reach this parse site; grepping both packages for `get_content_capturing_mode`/`CAPTURE_MESSAGE_CONTENT` turns up no independent parser in either. There is no functional casing bug here — the "OpenAI docs show lowercase, Anthropic docs show uppercase" discrepancy some users notice is a documentation-style difference only, and the value Honeybadger writes was already correct before this was verified.

**This case-insensitivity does not extend to Bedrock.** `opentelemetry-instrumentation-botocore`'s own content gate, `genai_capture_message_content()` in `extensions/bedrock_utils.py`, is a separate, unrelated parse site — a boolean literal check (`environ.get(..., "false").lower() == "true"`), not an `Enum[]` lookup. It opens only on the literal string `"true"`; `"span_only"` (the only value `_apply_env_gating()` ever writes) never satisfies it, in any case. See "Bedrock: explicit-only registration + the OTLP GenAI gate" below.

### `export="otlp"` also honors `insights_enabled`

The OTLP scrubbing exporter (`ScrubbingOTLPExporter.export` in `_bridge.py::make_otlp_exporter`) checks both `getattr(owner, "active", False)` and `honeybadger.config.insights_enabled` before scrubbing and forwarding spans, short-circuiting to `SpanExportResult.SUCCESS` (dropping the batch) if either is false. This was added so that flipping `insights_enabled` off at runtime silences OTLP export the same way it silences `llm.*` events in `_export_one` — without it, `export="otlp"` mode would have kept exporting spans as long as the OTel pipeline was attached, independent of the SDK's own insights toggle, which would have been a surprising inconsistency between the two export modes.

Scrubbing (`scrub_attributes`) only touches the content attributes gated in `_CONTENT_ATTRS` (`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions` — the last one gated on `include_prompts` too, since tool definitions are prompt-side content, not response content) — span events and every non-content attribute pass through to the OTLP endpoint unmodified. Content attributes that survive gating are normalized from the instrumentor's real `{"role": ..., "parts": [...]}` message shape into `{"content": ...}` (reusing `_semconv._flatten_parts`) before the content policy runs, so nested part text is truncated and non-text parts are replaced like the `events` export path. `make_otlp_exporter()` also reads `honeybadger.config.endpoint`/`api_key` once, at `init()` time, to build the wrapped `OTLPSpanExporter`; a later `honeybadger.configure(...)` call does not retarget an already-running OTLP pipeline — re-`init()` (via `tearDown()` + a fresh `LLMHoneybadger(export="otlp")`) if you need to change the endpoint or key. `max_event_bytes` is not applied in this mode — see the Configuration table.

### Owned vs. borrowed provider lifecycle; inert-after-teardown

- **Owned** (default, no `tracer_provider=` passed): `_build_provider()` creates a fresh `TracerProvider`. `_cleanup_wiring()` calls `force_flush()` then `shutdown()` on it at teardown.
- **Borrowed** (`LLMHoneybadger(tracer_provider=app_provider)`): we attach our context processor and exporter pipeline to the caller's provider and never shut *the provider* down — that's the app's to manage. OTel has no `remove_span_processor()` API, so on `tearDown()` our processor stays physically attached to the borrowed provider for its remaining lifetime. It goes **inert** rather than removed: `_cleanup_wiring()` force-flushes then `shutdown()`s *our own* `self._processor` (stopping its background batch-worker thread — otherwise repeated init/tearDown against one long-lived borrowed provider would accumulate live threads), which makes its `on_end()` a no-op for any span recorded afterward. `_export_one` and `ScrubbingOTLPExporter.export` also independently gate on `getattr(owner, "active", False)` (backed by `self._initialized`) as a second line of defense, so any span that somehow still reaches our exporter after teardown is silently dropped instead of producing a stale event. This is the same "wrapper gates on an initialized flag" pattern the Oban contrib uses for its telemetry handlers. The provider itself and any other processors attached to it are untouched — verified against the real SDK.

### The PyPI provenance trap

On PyPI, the *unprefixed* package names `opentelemetry-instrumentation-openai` and similarly-named packages for other providers are historically Traceloop's (openllmetry) packages, not OTel's. The official, OTel-governed successor family is `opentelemetry-instrumentation-genai-*` (`-genai-openai` here; `-genai-anthropic`, `-genai-langchain`, etc. for later phases) — different maintainer, different attribute dialect, different beta-stability posture. `setup.py`'s `[llm]` extra and `_INSTRUMENTORS` in `__init__.py` both pin to the `-genai-openai` name specifically. Any future provider addition (Phase 2+) must re-verify the publisher on PyPI before adding a dependency — matching the package name alone is not sufficient, since both families publish plausible-looking names.

### Bedrock: explicit-only registration + the OTLP GenAI gate

`_INSTRUMENTORS["bedrock"]` (module `opentelemetry.instrumentation.botocore`, class `BotocoreInstrumentor`) is registered like every other provider, but is also listed in `_EXPLICIT_ONLY = frozenset({"bedrock"})`. The registry comment states the reason directly:

```python
# BotocoreInstrumentor traces EVERY botocore call (S3, DynamoDB, ...),
# so bedrock is explicit-only: never part of auto-detection.
```

`_requested_instruments()`'s default branch (used by `auto_init()` and a bare `LLMHoneybadger()`) filters `_EXPLICIT_ONLY` out, so a process that merely has `boto3` installed never gets full-AWS-API tracing for free. A caller must pass `instruments=["bedrock"]` (or another explicit list naming it) to activate it — this is a deliberate deviation from "provider keys added to auto-detection," justified by the blast radius of instrumenting a module as ubiquitous as `botocore`.

Once activated, that same "instruments everything" property means the shared OTel pipeline can see non-GenAI spans (an S3 `GetObject`, a DynamoDB `PutItem`, an SQS call, etc.) interleaved with real Bedrock `converse`/`invoke_model` spans. Two different mechanisms keep those out of Honeybadger:
- The default `export="events"` path relies on `_semconv.py::normalize()` implicitly returning `None` for any span lacking a recognized `gen_ai.operation.name` — non-GenAI spans never reach `honeybadger.event()`.
- The `export="otlp"` path had no equivalent guard until it was added explicitly: `scrub_attributes()` in `_bridge.py` now drops any span whose attributes contain no key starting with `"gen_ai."`, immediately after the `disabled` check and before `_excluded()`. `make_otlp_exporter()`'s docstring documents why: "BotocoreInstrumentor traces every botocore call on the process (S3, DynamoDB, SQS, ...), and `export="otlp"` must never forward those non-GenAI spans to the configured OTel endpoint."

### The `opentelemetry-instrumentation-botocore==0.64b0` pin — do not casually bump

`setup.py`'s `[llm]` extra pins `opentelemetry-instrumentation-botocore` to the **exact** version `==0.64b0`, unlike every other genai dependency in that extra (which use open range pins, e.g. `>=1.0b0,<1.1`). The inline comment:

```python
# ==0.64b0 REQUIRED: 0.65b0 pins opentelemetry-instrumentation==0.65b0
# AND opentelemetry-semantic-conventions==0.65b0, both conflicting with
# the genai family's ~=0.64b0
```

In full: `opentelemetry-instrumentation-botocore==0.65b0` itself pins `opentelemetry-instrumentation` and `opentelemetry-semantic-conventions` to `==0.65b0`. But `opentelemetry-instrumentation-genai-openai` and `-genai-anthropic` (both `>=1.0b0,<1.1` in this same extra) require those two shared packages at `~=0.64b0`. Letting `opentelemetry-instrumentation-botocore` float to `0.65b0` breaks pip's resolver against the genai packages already pinned in this extra — the three families (`-botocore`, `-genai-openai`, `-genai-anthropic`) all transitively depend on `opentelemetry-instrumentation`/`opentelemetry-semantic-conventions`, and their version ranges don't currently overlap past `0.64b0`.

**If dependabot (or anyone) proposes bumping `opentelemetry-instrumentation-botocore` past `0.64b0`, do not accept it in isolation.** First check whether `opentelemetry-instrumentation-genai-openai`/`-genai-anthropic` have themselves shipped a release compatible with the newer `opentelemetry-instrumentation`/`opentelemetry-semantic-conventions` line — the botocore pin can only move once they have, and it will likely need to move in the same commit as their own version bumps, not on its own. Verify any bump with a **fresh, single-command** venv install (`pip install -e '.[llm]' 'anthropic>=0.51.0' boto3` in a throwaway venv, then `pip check`) before merging: installing in stages against an already-populated venv can produce misleading resolver warnings unrelated to this specific conflict (this happened during the initial packaging work — a two-phase install produced an unrelated `opentelemetry-exporter-otlp-proto-http`/`opentelemetry-sdk` warning that a from-scratch single-command install did not reproduce).

## The attribute matrix

Per-provider tables below, each built the same way: instrument real scenarios against a mocked/stubbed transport with the production env-gating path (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only`, `include_prompts=True, include_responses=True`), then dump the raw span attributes and/or the `data` dict passed to `honeybadger.event()`. These reflect the exact behavior of the pinned instrumentor versions — re-verify on any pin bump. Cells not exercised by any test are marked **unverified**, not assumed.

### OpenAI

Built in Task 10 by instrumenting four scenarios against `opentelemetry-instrumentation-genai-openai==1.0b0` (real instrumentor + `openai` SDK against a mocked httpx transport) with `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only` and `include_prompts=True, include_responses=True`, then dumping the full `data` dict passed to `honeybadger.event()`. This reflects the exact behavior of the pinned instrumentor version — re-verify on any pin bump.

| Schema field | sync non-stream | error (429) | streaming + `include_usage` | early-terminated stream |
|---|---|---|---|---|
| `provider` | present (`openai`) | present | present | present |
| `host` | present (`api.openai.com`) | present | present | present |
| `model` | present (`gpt-4o`, requested) | present | present | present |
| `response_model` | present (`gpt-4o-2024-08-06`) | absent | present (`gpt-4o`) | present (`gpt-4o`) |
| `duration` | present (int ms) | present | present | present |
| `input_tokens` | present (9) | absent | present (9) | absent |
| `output_tokens` | present (3) | absent | present (2) | absent |
| `cache_read_tokens` | unverified | unverified | unverified | unverified |
| `cache_creation_tokens` | unverified | unverified | unverified | unverified |
| `temperature` | unverified | unverified | unverified | unverified |
| `finish_reason` | present (`stop`) | absent | present (`stop`) | present (`error`) |
| `error` | absent | present (`RateLimitError`) | absent | absent |
| `prompts` | present (`[{role: user, content: hi}]`) | present | present | present |
| `response` | present (`[{role: assistant, content: "hello there"}]`) | absent (no response) | present (`"hello"`) | present (`"xx"`, partial content) |
| `provider_response_id` | present (`chatcmpl-test1`) | absent | present (`c`) | present (`c`) |
| `trace_id` | present (32-hex) | present | present | present |
| `streaming` | unpopulated in all modes at this pin | unpopulated in all modes at this pin | unpopulated in all modes at this pin | unpopulated in all modes at this pin |

Notes:

- **`streaming` is never populated by this instrumentor version.** Verified empirically that the raw span attributes, `span.name` (`"chat gpt-4o"`), and `span.kind` (`CLIENT`) are byte-for-byte identical between streaming and non-streaming chat calls at this pin — there is no signal on the span from which `_semconv.py` could derive a `streaming` boolean. This is a genuine "no source in this mode" case, not an adapter gap: `_semconv.py` has no mapping for it because there is currently nothing to map. Read this row as "unpopulated in all modes at this pin," not as a field that sometimes appears.
- **Error scenario (429 `RateLimitError`)**: `error.type` is set to the unqualified exception class name (`"RateLimitError"`), which `_semconv.py::_extract_error` picks up as the first-priority source (before exception span events or status description) — this instrumentor version does not record an `exception` span event, consistent with it using `error.type` via its `invocation.fail()` path rather than recording exceptions. Response-side fields (`response_model`, token counts, `finish_reason`, `provider_response_id`) are absent because the call failed before a response was received.
- **Early-terminated stream** (`stream.close()` before consuming all chunks): `finish_reason` reports the literal string `"error"`, not `"stop"` or absent. Token fields are absent (no usage chunk arrived before close, and `stream_options={"include_usage": True}` wasn't set for that scenario). Partial response content ("xx", 2 of 5 emitted chunks) is still present — the bridge emits whatever span arrives rather than suppressing partial data.
- **Exception raised mid-loop while consuming a stream** (consumer code raises inside the `for` loop, `with stream:` closes it deterministically as the exception propagates — not GC-dependent): behaves like the early-terminated-stream row (`finish_reason: "error"`, token fields absent, partial response content present) **except** `error` is populated with the raised exception's class name (e.g. `"ValueError"`), whereas the deliberate `.close()` case leaves `error` absent. An unconsumed/abandoned stream that's never closed and relies on GC to finalize the span was deliberately not integration-tested (flaky on GC timing) — see the plan's deferral list — but is expected to end the span only once garbage-collected, if at all.
- `cache_read_tokens`, `cache_creation_tokens`, and `temperature` were not exercised by any of the four brief scenarios (no cache usage or `temperature` param in the test requests) — they remain **unverified** against a live response, though `_semconv.py::_SCALAR_FIELDS` does have mappings for all three and there is no reason to expect them not to work when the corresponding usage/request attributes are present.
- `llm.embedding` fields (`provider`, `host`, `model`, `duration`, `input_tokens`, `error`) were not exercised in the Task 10 integration scenarios (all four scenarios are chat completions) — **unverified** against a live embeddings call, though the same `_semconv.py` scalar-field mapping applies.

### Anthropic

Built in the phase-2 plan's Task 3 by instrumenting real `opentelemetry-instrumentation-genai-anthropic==1.0b0` + the `anthropic` SDK against a mocked httpx transport, with the same production env-gating path as OpenAI, then dumping raw span attributes (via a `RecordingProcessor`) and the `data` dict passed to `honeybadger.event()`. See `honeybadger/tests/contrib/test_llm_anthropic.py`.

| Schema field | sync non-stream | error (429, `RateLimitError`) | streaming via `stream.text_stream` (bypass — see note) | streaming via direct iteration (control) | async non-stream |
|---|---|---|---|---|---|
| `provider` | present (`anthropic`) | unverified | unverified | unverified | present (`anthropic`) |
| `model` | present (`claude-sonnet-4-5`, requested) | unverified | present (`claude-sonnet-4-5`) | unverified | unverified |
| `host` | unverified (`server.address` observed on the raw span in the streaming scenario's request-side attributes, but no test asserts the `host` event field) | unverified | unverified | unverified | unverified |
| `duration` | present | unverified | unverified | unverified | unverified |
| `input_tokens` | present (**18** = 9 base + 2 cache_creation + 7 cache_read — instrumentor sums all three, see note) | absent (raw span has no usage attributes; call failed before a response) | absent | present (9) | present (18, same summing behavior) |
| `output_tokens` | present (3) | absent | absent | present (2) | unverified |
| `cache_read_tokens` | present (7) | unverified | unverified | unverified | present (7) |
| `cache_creation_tokens` | present (2) | unverified | unverified | unverified | present (2) |
| `finish_reason` | present (`stop`, mapped from `end_turn`) | unverified | absent | present (`stop`) | unverified |
| `error` | unverified | present (`RateLimitError`) | absent for a deliberate `stream.close()`; present (`ValueError`, the raised exception's class name) when a consumer exception instead propagates out of the loop (separate scenario, same bypass) | unverified | unverified |
| `prompts` | present | unverified | unverified | unverified | unverified |
| `response` | present | unverified | absent (never populated — see note) | present (`[{"role": "assistant", "content": "hello"}]`) | unverified |
| `provider_response_id` | unverified | unverified | unverified | unverified | unverified |
| `trace_id` | unverified | unverified | unverified | unverified | unverified |
| `temperature` | unverified in this scenario (not requested) — confirmed mapped (0.7) in a dedicated request-param scenario, see note below | — | — | — | — |

Notes:

- **`stream.text_stream` never populates response-side telemetry (significant, non-obvious finding).** Consuming a streaming response via `stream.text_stream` — the Anthropic SDK's own documented convenience API for text-only consumption — never populates `gen_ai.usage.*`, `gen_ai.output.messages`, or `gen_ai.response.*` on the span, even when the stream is read to full completion. Root cause: `MessageStream.text_stream` (`anthropic/lib/streaming/_messages.py`) is a generator bound at `__init__` time directly against the **unwrapped** inner SDK stream object; the OTel `MessagesStreamWrapper`'s own `__iter__`/`__next__` (which drives `_process_chunk`, the accumulation hook the instrumentor needs) is never invoked when consuming via `.text_stream`. The span still ends and the event still fires (request-side fields — `provider`, `model`, `prompts` — are present), but response-side telemetry is silently absent. Confirmed with a control test (`test_streaming_direct_iteration_populates_response_data`): iterating the wrapper directly (`for chunk in stream: ...`) against an identical mocked SSE body populates everything correctly. **Any application code consuming Anthropic streams via `stream.text_stream` gets essentially no usage/cost/finish_reason telemetry from this instrumentor version.** `get_final_message()`/`get_final_text()` as a third consumption path were not tested — plausible they share the same bypass, not confirmed (unverified).
- **`stop_sequence` collapse.** A response that stopped because it hit a configured stop sequence (`stop_reason: "stop_sequence"`) normalizes to the exact same `finish_reason: "stop"` as a natural `end_turn` completion — indistinguishable in both the raw span and the event. The actual matched stop-sequence string (the response's `stop_sequence` field) is never captured anywhere, raw or event; only the *request's* configured `stop_sequences` list reaches the raw span (`gen_ai.request.stop_sequences`), and even that doesn't reach the event (next note). This is an upstream omission, not an adapter gap.
- **Request-param scalars intentionally unmapped (frozen schema).** `gen_ai.request.max_tokens`, `.stop_sequences`, `.top_k`, and `.top_p` are present on the raw span (confirmed with `max_tokens=64, temperature=0.7, top_p=0.9, top_k=40, stop_sequences=["X"]` on the request) but absent from the emitted event; only `gen_ai.request.temperature` is mapped, by `_semconv.py::_SCALAR_FIELDS`. This is not an oversight: none of the other four fields exist in the frozen `llm.chat` event schema, so mapping them would mean inventing new event fields — deliberately left unmapped pending a spec amendment, not a bug.
- **`tool_use`/`thinking` content parts: unverified.** Not exercised by any test. `_flatten_parts` (`_semconv.py`) is, by code inspection, tolerant of non-text parts (falls through to returning the raw parts list rather than raising or dropping data), but no test drives a `tool_use` or `thinking` response through the mocked transport to confirm end-to-end behavior.
- **Async streaming (`AsyncAnthropic` + `messages.stream`): unverified.** Not exercised by any test; only async non-streaming `messages.create` was tested.
- Async (`AsyncAnthropic.messages.create`) exhibits the same cache-token-summing behavior as sync (`input_tokens: 18`, same `cache_read_tokens`/`cache_creation_tokens`) — same underlying code path (`messages_extractors.extract_usage_tokens`).

### Bedrock (metadata-only)

Bedrock integration is **metadata-only**: `instruments=["bedrock"]` (via `opentelemetry-instrumentation-botocore==0.64b0`) produces usable spans with rich metadata for `converse()` and `invoke_model()`, but message content (`prompts`/`response`) is categorically unavailable through this architecture at this pin, regardless of `include_prompts`/`include_responses` or the content-capture env var's value. See "Bedrock: explicit-only registration + the OTLP GenAI gate" above for why `bedrock` requires `instruments=["bedrock"]` explicitly, and the Limitations section below for the full content-unavailability chain.

Built by instrumenting real `BotocoreInstrumentor` against a `botocore.stub.Stubber`-stubbed `bedrock-runtime` client, with the same production env-gating path. See `honeybadger/tests/contrib/test_llm_bedrock.py`.

| Schema field | `converse` (success) | `converse` (error, `ThrottlingException` 429) | `invoke_model` (success, Anthropic-on-Bedrock body dialect) |
|---|---|---|---|
| `provider` | present (`aws.bedrock`, from the legacy `gen_ai.system` attribute — no `gen_ai.provider.name` at this pin) | unverified | present (`aws.bedrock`) |
| `model` | present (model ID, e.g. `anthropic.claude-3-5-sonnet-20241022-v2:0`) | unverified | present (same model ID) |
| `duration` | present | unverified | unverified |
| `input_tokens` | present (9) | absent | present (9) |
| `output_tokens` | present (3) | absent | present (3) |
| `finish_reason` | present (`end_turn` — **not** normalized to `"stop"` at this pin; the raw stop-reason value passes through as-is, unlike the Anthropic-direct instrumentor's mapping) | unverified | present (`end_turn`) |
| `error` | unverified | present (`ThrottlingException`) | unverified |
| `prompts` | absent (confirmed even with `include_prompts=True` and the production `span_only` env-gating path) | absent | absent |
| `response` | absent (same conditions) | absent | absent |
| `max_tokens` (request param) | unverified | unverified | raw-present (64), event-absent — same frozen-schema rule as Anthropic (no `_semconv.py` mapping) |
| `converse_stream` | **entirely unverified** — `botocore.stub.Stubber` cannot fake the `EventStream` wire format `ConverseStreamWrapper` expects (it requires a single dict response, not a simulated multi-event stream); judged not practically stubbable without an HTTP-level mock, out of scope. No test exists. | — | — |

Notes:

- Content is unavailable for **two independent, both-confirmed reasons**: (1) Bedrock's own content gate, `genai_capture_message_content()` in `extensions/bedrock_utils.py`, only recognizes the literal string `"true"` — the `"span_only"` value Honeybadger's `_apply_env_gating()` writes never opens it, at any casing; (2) even when that gate is forced open (setting `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` to the literal `"true"` directly, bypassing Honeybadger's normal gating), content is emitted via `instrumentor_context.logger.emit(...)` onto the OTel **logs** signal, never `span.set_attribute()` — and `_activate_instrumentors` calls `BotocoreInstrumentor.instrument(tracer_provider=provider)` with no `logger_provider=`, so those log records go to a logger provider Honeybadger never configures. The span-only bridge has no logs pipeline at all. Confirmed with a dedicated test, `test_content_capture_never_reaches_event_even_when_bedrocks_own_gate_is_forced_on`, that forces the gate open and still finds content absent from both the raw span and the emitted event.
- `gen_ai.operation.name` is present as `"chat"` for both `converse` and `invoke_model` at this pin, so no `_OPERATION_EVENT_TYPES` extension was needed to route Bedrock spans into the `llm.chat` event type.
- No `_semconv.py` changes were needed for the metadata path — the existing `gen_ai.system -> provider` legacy-dialect fallback (originally added for OpenAI's own legacy dialect) already covers Bedrock's `"aws.bedrock"` value.

## Configuration

`honeybadger.config.insights_config.llm` is an `LLMConfig` dataclass:

| Field | Default | Effect |
|---|---|---|
| `disabled` | `False` | When `True`, `auto_init()` skips activation entirely; also re-checked at emit time in `_export_one` and `ScrubbingOTLPExporter.export`, so flipping it after `init()` stops events without a `tearDown()`. |
| `include_prompts` | `False` | Opt-in: include the `prompts` field (post content-policy) in `llm.chat` events, and (together with `include_responses`) trigger `span_only` content-capture env gating at `init()`. |
| `include_responses` | `False` | Opt-in: include the `response` field (post content-policy) in `llm.chat` events; same env-gating trigger as `include_prompts`. |
| `max_content_length` | `8192` | Per-content-string character cap; longer strings are truncated with a `"... [TRUNCATED]"` marker (`_policy.py::_truncate_message`). |
| `max_event_bytes` | `65536` | Serialized event byte budget (UTF-8), **`export="events"` mode only**. Oldest prompt messages are dropped first (keeping a leading system message when present); if the event is still over budget, remaining prompts (including the preserved system message) are dropped, then the response; `content_dropped: True` is set whenever anything was dropped (`_policy.py::enforce_event_budget`). Metadata-only events that still exceed the budget are left as-is (the `EventsWorker`/API limits are the backstop from there). `export="otlp"` re-exports full OTel spans to `/v1/traces` and does **not** apply this budget at all — span/attribute size limits are whatever the OTLP endpoint enforces. |
| `exclude_models` | `[]` | List of exact strings or compiled regexes (matched via `.search()`), checked against the *requested* model. Filters `llm.*` events / OTLP-mode spans only — does not stop the instrumentor from running. |

`LLMHoneybadger(instruments=None, tracer_provider=None, export="events")` is the constructor: `instruments` restricts/selects which provider instrumentors to activate (default: auto-detect all known providers whose SDK is importable — currently only `"openai"`); `tracer_provider` is the borrowed-provider escape hatch; `export` is `"events"` (default, `llm.*` Insights events) or `"otlp"` (scrubbed spans to Honeybadger's OTel endpoint, requires the separately-installed `opentelemetry-exporter-otlp-proto-http` package).

## Limitations

- **No embedding input content.** The pinned instrumentor does not capture embedding input text at all — `llm.embedding` events carry `provider`, `host`, `model`, `duration`, `input_tokens`, and `error` only, regardless of `include_prompts`. There is nothing for the content policy to gate.
- **Provider misattribution for OpenAI-compatible endpoints.** `provider` (from `gen_ai.provider.name`) follows the instrumentor's SDK-based classification, not the actual endpoint — a call routed to an OpenAI-compatible third-party API (Azure, Groq, a local proxy, etc.) may still report `provider: "openai"`. `host` (from `server.address`, e.g. `api.openai.com`) is included specifically so queries can disambiguate by endpoint when `provider` can't be trusted.
- **Free-form prompt text cannot be key-filtered.** The content policy's structural redaction (`filter_structure`, honoring `params_filters`) operates on message *keys*, not on secrets embedded inside prompt or response *text*. A user who pastes an API key into a chat message will have it logged verbatim if `include_prompts`/`include_responses` are on — this risk is inherent to opting into content capture, not a bug in the filter.
- **Streaming usage requires `include_usage`.** OpenAI Chat Completions streams only report token counts when the caller passes `stream_options={"include_usage": True}`; without it, `input_tokens`/`output_tokens` are absent from streaming `llm.chat` events (confirmed in the early-terminated-stream row of the attribute matrix above, where `stream_options` wasn't set).
- **`streaming` field is currently dead.** See the attribute matrix note above — not populated for any mode at the pinned instrumentor version. Revisit if a future instrumentor minor adds a span-level signal.
- **Python ≥3.10 required for the `[llm]` extra.** `setup.py`'s `extras_require["llm"]` uses `python_version >= "3.10"` environment markers on both `opentelemetry-sdk` and `opentelemetry-instrumentation-genai-openai`, matching that package's real floor. `pip install honeybadger[llm]` succeeds on any supported interpreter (3.9+, matching the core package's `python_requires`), but on 3.9 the markers skip the deps entirely and `LLMHoneybadger().init()` raises `ImportError` naming the extra and the floor. `auto_init()` never reaches that `ImportError` in the first place: it calls `_otel_available()`'s `find_spec` check up front and returns `None` immediately when the deps aren't importable, short-circuiting before `init()` (and its `ImportError`) is ever invoked — no exception is raised or caught. So plain scripts and Django/Flask/ASGI apps on 3.9 are unaffected when the extra isn't installed.
- **Anthropic streaming via `stream.text_stream` reports no usage/finish_reason telemetry.** The event still fires with request-side fields, but token counts, `finish_reason`, and `response` are silently absent — see the Anthropic attribute matrix note above for the root cause and the direct-iteration workaround. Not a bug in the Honeybadger bridge; it's an upstream consumption-path issue in `opentelemetry-instrumentation-genai-anthropic==1.0b0`.
- **Anthropic request-param scalars (`max_tokens`, `stop_sequences`, `top_k`, `top_p`) never reach `llm.chat` events.** Present on the raw span, intentionally left unmapped because none of them exist in the frozen `llm.chat` event schema — mapping them would mean inventing new event fields, which requires a spec amendment. `temperature` is the one request-param scalar that is mapped, since it's the one already defined in the schema.
- **Anthropic `tool_use`/`thinking` content parts are unverified.** No test drives a tool-use or extended-thinking response through the instrumentor; behavior is untested end-to-end (see the Anthropic attribute matrix above).
- **Anthropic async streaming is unverified.** No test exercises `AsyncAnthropic` + `messages.stream`; only async non-streaming `messages.create` was tested.
- **Bedrock content capture unavailable at this pin.** `instruments=["bedrock"]` (via `opentelemetry-instrumentation-botocore==0.64b0`) captures rich metadata (`provider` from the legacy `gen_ai.system` attribute, `model`, tokens, `finish_reason`, `error`) for both `converse()` and `invoke_model()`, but never message content, regardless of `include_prompts`/`include_responses` — the instrumentor emits content on the OTel **logs** signal (`instrumentor_context.logger.emit(...)` in `extensions/bedrock.py`), which the span-only bridge does not consume. Compounding this, Bedrock's own content gate (`genai_capture_message_content()` in `extensions/bedrock_utils.py`) only recognizes the literal string `"true"`, not the `"span_only"` value `_apply_env_gating()` sets — so it never opens via Honeybadger's normal env-gating path at all. `bedrock` is also never auto-detected (see "Bedrock: explicit-only registration…" above) — it must be requested explicitly via `instruments=["bedrock"]`. `converse_stream` is entirely untested — `botocore.stub.Stubber` can't fake the `EventStream` wire format it needs. See the Bedrock attribute matrix above and `honeybadger/tests/contrib/test_llm_bedrock.py` for the observed evidence and regression tests; `_semconv.py` needed no changes for the metadata path.
- **One instance per process.** A second `LLMHoneybadger().init()` from a different instance raises `RuntimeError` until the first is torn down. Tests must `tearDown()` between cases.
- **Concurrent instrumentation from multiple libraries is unsupported.** `init()` checks each instrumentor's `is_instrumented_by_opentelemetry` flag and skips (with a warning) any provider already instrumented by another consumer in the process, but this detection is best-effort — true concurrent init races are not guarded against.
