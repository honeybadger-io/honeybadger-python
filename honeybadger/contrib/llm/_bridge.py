"""Span -> Honeybadger event bridge.

export_spans() is pure Python (no otel imports) so it unit-tests against
duck-typed spans and the module imports without the [llm] extra. The
make_*() factories build real otel SpanProcessor/SpanExporter subclasses
and import opentelemetry lazily inside the function bodies.
"""

import logging
from typing import Set

from honeybadger import honeybadger
from ._semconv import normalize, _flatten_parts
from ._policy import apply_content_policy, enforce_event_budget

logger = logging.getLogger(__name__)

CONTEXT_ATTR_PREFIX = "honeybadger.context."

_warned_failure_classes: Set[str] = set()


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

    # Lift honeybadger.context.* BEFORE budgeting so lifted context counts
    # against max_event_bytes too -- otherwise a large context value could
    # push the serialized event back over budget after enforcement.
    for key, value in (span.attributes or {}).items():
        if key.startswith(CONTEXT_ATTR_PREFIX):
            data.setdefault(key[len(CONTEXT_ATTR_PREFIX) :], value)

    data = enforce_event_budget(data, llm_config.max_event_bytes)

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


def make_context_processor(owner):
    from opentelemetry.sdk.trace import SpanProcessor  # type: ignore[import-not-found]

    class HoneybadgerContextSpanProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            # Gate on owner.active (like the exporters): on a borrowed
            # provider this processor stays attached forever (otel has no
            # remove_span_processor API), so after tearDown() it must go
            # inert instead of injecting honeybadger.context.* attributes
            # into the app's unrelated spans.
            if not getattr(owner, "active", False):
                return
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
    spans; defaults to a real OTLPSpanExporter targeting Honeybadger."""
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
