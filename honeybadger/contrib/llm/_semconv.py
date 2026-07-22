"""Versioned adapter: current OTel GenAI semconv span -> normalized event fields.

All attribute knowledge for the "events" export mode lives here. Every
attribute is optional; absent sources mean absent fields (never None).
No opentelemetry imports — operates on the ReadableSpan duck-type
(attributes, events, status, start_time, end_time, get_span_context).
"""

import datetime
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
    "gen_ai.conversation.id": "conversation_id",
}


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

    operation: Any = attributes.get("gen_ai.operation.name")
    event_type = _OPERATION_EVENT_TYPES.get(operation, "llm.call")  # type: ignore[arg-type]

    data: Dict[str, Any] = {}
    for attr, field_name in _SCALAR_FIELDS.items():
        if attr in attributes and field_name not in data:
            data[field_name] = attributes[attr]

    finish_reasons = attributes.get("gen_ai.response.finish_reasons")
    if finish_reasons:
        # Normally a sequence (tuple/list); guard against a bare string,
        # which is itself iterable and would otherwise yield its first
        # character instead of the whole reason (e.g. "s" from "stop").
        data["finish_reason"] = (
            finish_reasons
            if isinstance(finish_reasons, str)
            else list(finish_reasons)[0]
        )

    duration = _duration_ms(span)
    if duration is not None:
        data["duration"] = duration

    trace_id = _trace_id(span)
    if trace_id:
        data["trace_id"] = trace_id

    span_id = _span_id(span)
    if span_id:
        data["span_id"] = span_id
    parent_span_id = _parent_span_id(span)
    if parent_span_id:
        data["parent_span_id"] = parent_span_id
    ts = _start_ts(span)
    if ts is not None:
        data["ts"] = ts

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
