"""Span -> Honeybadger event bridge.

export_spans() is pure Python (no otel imports) so it unit-tests against
duck-typed spans and the module imports without the [llm] extra. The
make_*() factories build real otel SpanProcessor/SpanExporter subclasses
and import opentelemetry lazily inside the function bodies.
"""

import logging
from collections import OrderedDict
from typing import Set

from honeybadger import honeybadger
from ._semconv import normalize, _flatten_parts, FRAMEWORK_EVENT_TYPES
from ._policy import (
    apply_content_policy,
    apply_opaque_content_policy,
    enforce_event_budget,
)

logger = logging.getLogger(__name__)

CONTEXT_ATTR_PREFIX = "honeybadger.context."

_warned_failure_classes: Set[str] = set()

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
    from opentelemetry.sdk.trace import SpanProcessor  # type: ignore[import-not-found]

    class HoneybadgerContextSpanProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            snapshot_context_attributes(span)

    return HoneybadgerContextSpanProcessor()


def make_events_exporter(owner):
    from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
        SpanExporter,
        SpanExportResult,
    )

    class HoneybadgerLLMSpanExporter(SpanExporter):
        def export(self, spans):
            export_spans(spans, owner)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    return HoneybadgerLLMSpanExporter()


_CONTENT_ATTRS = {
    "gen_ai.input.messages": "include_prompts",
    "gen_ai.system_instructions": "include_prompts",
    "gen_ai.tool.definitions": "include_prompts",
    "gen_ai.output.messages": "include_responses",
}


def scrub_attributes(attributes, llm_config, params_filters):
    """Return a new, content-policied attributes dict for OTLP export,
    or None when the span must not be exported at all."""
    if llm_config.disabled:
        return None
    if not any(key.startswith("gen_ai.") for key in attributes):
        # GenAI classification gate (bedrock containment): botocore
        # instruments every AWS call (S3, DynamoDB, SQS, ...), not just
        # Bedrock. A span with no gen_ai.* attribute at all is not an LLM
        # call and must never reach the OTLP endpoint.
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
        return _json.dumps("[unparseable content removed]")
    if not isinstance(decoded, list):
        decoded = [decoded]
    messages = [_normalize_content_entry(m) for m in decoded]
    policied = apply_content_policy(messages, params_filters, max_content_length)
    return _json.dumps(policied, ensure_ascii=False, default=repr)


def _normalize_content_entry(entry):
    """Flatten the instrumentor's real `{"role": ..., "parts": [...]}`
    message shape into the `{"content": ...}` shape apply_content_policy()
    understands, reusing the same _flatten_parts() the events path
    (_semconv.py) uses. Entries without a "parts" key (e.g. the flat
    part-dicts gen_ai.system_instructions emits) pass through unchanged."""
    if not isinstance(entry, dict):
        return {"content": entry}
    if "parts" not in entry:
        return entry
    entry = dict(entry)
    entry["content"] = _flatten_parts(entry.pop("parts"))
    return entry


def make_otlp_exporter(owner, wrapped=None):
    """Build the scrubbing OTLP exporter. `wrapped` is the real (or, for
    tests, a recording stand-in) SpanExporter that receives cloned/scrubbed
    spans; defaults to a real OTLPSpanExporter targeting Honeybadger.

    GenAI classification gate: any span whose attributes contain no key
    starting with "gen_ai." is dropped before it ever reaches `wrapped`
    (see scrub_attributes()). This matters once "bedrock" is activated --
    BotocoreInstrumentor traces every botocore call on the process (S3,
    DynamoDB, SQS, ...), and export="otlp" must never forward those
    non-GenAI spans to the configured OTel endpoint."""
    import importlib.util

    if wrapped is None and (
        importlib.util.find_spec("opentelemetry.exporter.otlp.proto.http") is None
    ):
        raise ImportError(
            "export='otlp' requires opentelemetry-exporter-otlp-proto-http "
            "(not part of the honeybadger[llm] extra): "
            "pip install opentelemetry-exporter-otlp-proto-http"
        )
    from opentelemetry.sdk.trace import ReadableSpan  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
        SpanExporter,
        SpanExportResult,
    )

    config = honeybadger.config
    if wrapped is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter,
        )

        wrapped = OTLPSpanExporter(
            endpoint=config.endpoint.rstrip("/") + "/v1/traces",
            headers={"X-API-Key": config.api_key},
        )

    class ScrubbingOTLPExporter(SpanExporter):
        def export(self, spans):
            if not getattr(owner, "active", False):
                return SpanExportResult.SUCCESS
            if not honeybadger.config.insights_enabled:
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
