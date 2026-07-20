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

## Why the non-obvious choices

### Private `TracerProvider`

Instrumentor patches are process-global (the instrumentor monkeypatches `openai` SDK methods), but a private `TracerProvider` controls *where the resulting spans go* independent of that. `_build_provider()` creates a fresh `TracerProvider()` for an owned instance so our export pipeline never depends on — or interferes with — an application's own OTel setup. The `tracer_provider=` constructor arg is the escape hatch for apps that want to add our processor to their own provider instead (see "owned vs. borrowed" below).

### Batch exporter off the hot path; `SimpleSpanProcessor` under Lambda

`_attach_pipeline` normally installs a `BatchSpanProcessor`, so span normalization, content policy, and the Honeybadger event push all happen on the batch processor's background thread — the application thread calling the OpenAI SDK only pays the instrumentor's attribute-collection cost, not ours.

Under `honeybadger.config.is_aws_lambda_environment`, `_attach_pipeline` uses `SimpleSpanProcessor(exporter)` instead — synchronous, on the calling thread. A frozen Lambda execution environment between invocations can suspend the batch thread mid-flush and silently lose buffered spans; going synchronous accepts the small per-call latency cost in exchange for not losing data, mirroring the intent of the SDK's existing `force_sync` default for Lambda notices (though `force_sync` itself is a separate knob that only affects notice delivery, not events).

### `honeybadger.context.*` span-attribute snapshot

`honeybadger.event_context()` lives in a `ContextVar` scoped to the calling thread. The batch processor's export thread runs later, disconnected from that thread — reading `_get_event_context()` at export time would see an empty context. `make_context_processor()` installs a `SpanProcessor` whose `on_start` calls `snapshot_context_attributes(span)` *while still on the calling thread*, copying scalar (`str`/`int`/`float`/`bool`) context values onto the span as `honeybadger.context.<key>` attributes. `_export_one` later reads them back off the span (`span.attributes`) and merges them into the event data with `setdefault` — span-derived fields win on key collision, context fields fill in the rest. The same processor is attached regardless of `export` mode, so `export="otlp"` spans carry the same `honeybadger.context.*` attributes for OTLP-side correlation.

### `provider_response_id` naming, not `request_id`

`honeybadger.event(event_type, data)` builds `final_payload = {**self._get_event_context(), **payload}` in `core.py` — event-context keys are merged first (lower precedence), explicit payload keys win on collision. The response-object ID from `gen_ai.response.id` is deliberately named `provider_response_id` rather than `request_id`: `request_id` is Honeybadger's own request-correlation field, populated from event context, and if the span-derived data used that same name it would silently clobber the correlation ID it's supposed to sit alongside, rather than simply refusing to overwrite it. A distinctive name sidesteps the collision entirely instead of relying on merge-order behavior to save it.

### Env-gating rules and restore-on-`tearDown`

The instrumentor gates message-content capture with one process-global env var, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`, read once at instrument time — it can't represent our two independent `include_prompts`/`include_responses` flags. `_apply_env_gating()` (called before `_activate_instrumentors`, since the instrumentor reads the gate at instrument time):
- Never overrides a value the user has already set (checks `CONTENT_ENV_VAR in os.environ` first and returns immediately if present).
- Otherwise sets it to `"span_only"` iff `include_prompts or include_responses`, and records `self._env_was_set_by_us = True` so `tearDown()` knows it owns the value.

`_restore_env_gating()` (called from `_cleanup_wiring()`, so it runs both on a clean `tearDown()` and on a failed `init()`) pops the env var **only if `self._env_was_set_by_us` is true** — if the user set it themselves, or if we never touched it (content flags were off), teardown leaves it exactly as found. The exporter also independently enforces `include_prompts`/`include_responses` per-event regardless of the env var's state, since the env var is a blunt, process-wide switch and the two flags are the fine-grained ones.

### `export="otlp"` also honors `insights_enabled`

The OTLP scrubbing exporter (`ScrubbingOTLPExporter.export` in `_bridge.py::make_otlp_exporter`) checks both `getattr(owner, "active", False)` and `honeybadger.config.insights_enabled` before scrubbing and forwarding spans, short-circuiting to `SpanExportResult.SUCCESS` (dropping the batch) if either is false. This was added so that flipping `insights_enabled` off at runtime silences OTLP export the same way it silences `llm.*` events in `_export_one` — without it, `export="otlp"` mode would have kept exporting spans as long as the OTel pipeline was attached, independent of the SDK's own insights toggle, which would have been a surprising inconsistency between the two export modes.

### Owned vs. borrowed provider lifecycle; inert-after-teardown

- **Owned** (default, no `tracer_provider=` passed): `_build_provider()` creates a fresh `TracerProvider`. `_cleanup_wiring()` calls `force_flush()` then `shutdown()` on it at teardown.
- **Borrowed** (`LLMHoneybadger(tracer_provider=app_provider)`): we attach our context processor and exporter pipeline to the caller's provider and never shut it down — that's the app's to manage. OTel has no `remove_span_processor()` API, so on `tearDown()` our processor stays physically attached to the borrowed provider for its remaining lifetime. It goes **inert** rather than removed: `_export_one` and `ScrubbingOTLPExporter.export` both gate on `getattr(owner, "active", False)` (backed by `self._initialized`), so any span that still reaches our exporter after teardown is silently dropped instead of producing a stale event. This is the same "wrapper gates on an initialized flag" pattern the Oban contrib uses for its telemetry handlers.

### The PyPI provenance trap

On PyPI, the *unprefixed* package names `opentelemetry-instrumentation-openai` and similarly-named packages for other providers are historically Traceloop's (openllmetry) packages, not OTel's. The official, OTel-governed successor family is `opentelemetry-instrumentation-genai-*` (`-genai-openai` here; `-genai-anthropic`, `-genai-langchain`, etc. for later phases) — different maintainer, different attribute dialect, different beta-stability posture. `setup.py`'s `[llm]` extra and `_INSTRUMENTORS` in `__init__.py` both pin to the `-genai-openai` name specifically. Any future provider addition (Phase 2+) must re-verify the publisher on PyPI before adding a dependency — matching the package name alone is not sufficient, since both families publish plausible-looking names.

## The attribute matrix

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
- `cache_read_tokens`, `cache_creation_tokens`, and `temperature` were not exercised by any of the four brief scenarios (no cache usage or `temperature` param in the test requests) — they remain **unverified** against a live response, though `_semconv.py::_SCALAR_FIELDS` does have mappings for all three and there is no reason to expect them not to work when the corresponding usage/request attributes are present.
- `llm.embedding` fields (`provider`, `host`, `model`, `duration`, `input_tokens`, `error`) were not exercised in the Task 10 integration scenarios (all four scenarios are chat completions) — **unverified** against a live embeddings call, though the same `_semconv.py` scalar-field mapping applies.

## Configuration

`honeybadger.config.insights_config.llm` is an `LLMConfig` dataclass:

| Field | Default | Effect |
|---|---|---|
| `disabled` | `False` | When `True`, `auto_init()` skips activation entirely; also re-checked at emit time in `_export_one` and `ScrubbingOTLPExporter.export`, so flipping it after `init()` stops events without a `tearDown()`. |
| `include_prompts` | `False` | Opt-in: include the `prompts` field (post content-policy) in `llm.chat` events, and (together with `include_responses`) trigger `span_only` content-capture env gating at `init()`. |
| `include_responses` | `False` | Opt-in: include the `response` field (post content-policy) in `llm.chat` events; same env-gating trigger as `include_prompts`. |
| `max_content_length` | `8192` | Per-content-string character cap; longer strings are truncated with a `"... [TRUNCATED]"` marker (`_policy.py::_truncate_message`). |
| `max_event_bytes` | `65536` | Serialized event byte budget (UTF-8). Oldest prompt messages are dropped (keeping a leading system message when present) until the event fits; `content_dropped: True` is set when anything was dropped (`_policy.py::enforce_event_budget`). |
| `exclude_models` | `[]` | List of exact strings or compiled regexes (matched via `.search()`), checked against the *requested* model. Filters `llm.*` events / OTLP-mode spans only — does not stop the instrumentor from running. |

`LLMHoneybadger(instruments=None, tracer_provider=None, export="events")` is the constructor: `instruments` restricts/selects which provider instrumentors to activate (default: auto-detect all known providers whose SDK is importable — currently only `"openai"`); `tracer_provider` is the borrowed-provider escape hatch; `export` is `"events"` (default, `llm.*` Insights events) or `"otlp"` (scrubbed spans to Honeybadger's OTel endpoint, requires the separately-installed `opentelemetry-exporter-otlp-proto-http` package).

## Limitations

- **No embedding input content.** The pinned instrumentor does not capture embedding input text at all — `llm.embedding` events carry `provider`, `host`, `model`, `duration`, `input_tokens`, and `error` only, regardless of `include_prompts`. There is nothing for the content policy to gate.
- **Provider misattribution for OpenAI-compatible endpoints.** `provider` (from `gen_ai.provider.name`) follows the instrumentor's SDK-based classification, not the actual endpoint — a call routed to an OpenAI-compatible third-party API (Azure, Groq, a local proxy, etc.) may still report `provider: "openai"`. `host` (from `server.address`, e.g. `api.openai.com`) is included specifically so queries can disambiguate by endpoint when `provider` can't be trusted.
- **Free-form prompt text cannot be key-filtered.** The content policy's structural redaction (`filter_structure`, honoring `params_filters`) operates on message *keys*, not on secrets embedded inside prompt or response *text*. A user who pastes an API key into a chat message will have it logged verbatim if `include_prompts`/`include_responses` are on — this risk is inherent to opting into content capture, not a bug in the filter.
- **Streaming usage requires `include_usage`.** OpenAI Chat Completions streams only report token counts when the caller passes `stream_options={"include_usage": True}`; without it, `input_tokens`/`output_tokens` are absent from streaming `llm.chat` events (confirmed in the early-terminated-stream row of the attribute matrix above, where `stream_options` wasn't set).
- **`streaming` field is currently dead.** See the attribute matrix note above — not populated for any mode at the pinned instrumentor version. Revisit if a future instrumentor minor adds a span-level signal.
- **Python ≥3.10 required for the `[llm]` extra.** `setup.py`'s `extras_require["llm"]` uses `python_version >= "3.10"` environment markers on both `opentelemetry-sdk` and `opentelemetry-instrumentation-genai-openai`, matching that package's real floor. `pip install honeybadger[llm]` succeeds on any supported interpreter (3.9+, matching the core package's `python_requires`), but on 3.9 the markers skip the deps entirely and `LLMHoneybadger().init()` raises `ImportError` naming the extra and the floor. `auto_init()` swallows that `ImportError` silently (via `_otel_available()`'s `find_spec` check) so plain scripts and Django/Flask/ASGI apps on 3.9 are unaffected when the extra isn't installed.
- **One instance per process.** A second `LLMHoneybadger().init()` from a different instance raises `RuntimeError` until the first is torn down. Tests must `tearDown()` between cases.
- **Concurrent instrumentation from multiple libraries is unsupported.** `init()` checks each instrumentor's `is_instrumented_by_opentelemetry` flag and skips (with a warning) any provider already instrumented by another consumer in the process, but this detection is best-effort — true concurrent init races are not guarded against.
