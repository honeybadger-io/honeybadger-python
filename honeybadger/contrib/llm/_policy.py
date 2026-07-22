"""Content policy for LLM events. Normative order (spec):
part-drop -> structural redaction -> per-string truncation -> byte budget.
apply_content_policy is pure and never mutates its inputs.
enforce_event_budget mutates and returns the passed event dict in place.
"""

import json
from typing import Optional

from honeybadger.utils import filter_structure

TRUNCATION_MARKER = "... [TRUNCATED]"
OMITTED_PART = "[non-text content omitted]"


def apply_content_policy(
    messages: Optional[list], filter_keys: list, max_content_length: int
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
            (
                part
                if isinstance(part, str)
                else (
                    part.get("content")
                    if isinstance(part, dict) and part.get("type") == "text"
                    else OMITTED_PART
                )
            )
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
            (
                part[:max_length] + TRUNCATION_MARKER
                if isinstance(part, str) and len(part) > max_length
                else part
            )
            for part in content
        ]
    return message


def apply_opaque_content_policy(value, filter_keys: list, max_content_length: int):
    """Policy for any-typed opaque content (tool arguments/results, workflow
    input/output). JSON-decode when the value is a JSON string, key-redact
    any structure, truncate EVERY string leaf (not just "content" keys).
    Pure -- never mutates its input. Returns a JSON-serializable value."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass  # plain string content: truncate below
    if isinstance(value, tuple):
        # OTel stores sequence-valued attrs as tuples; without this a
        # top-level tuple would skip both filter_structure() (redaction)
        # and the isinstance(value, list) check in _truncate_string_leaves,
        # bypassing the policy entirely.
        value = list(value)
    if isinstance(value, (dict, list)):
        value = filter_structure(value, filter_keys)
    return _truncate_string_leaves(value, max_content_length)


def _truncate_string_leaves(value, max_length: int):
    if isinstance(value, str):
        if len(value) > max_length:
            return value[:max_length] + TRUNCATION_MARKER
        return value
    if isinstance(value, list):
        return [_truncate_string_leaves(item, max_length) for item in value]
    if isinstance(value, dict):
        return {
            key: _truncate_string_leaves(item, max_length)
            for key, item in value.items()
        }
    return value


def _size(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False, default=repr).encode("utf-8"))


def enforce_event_budget(data: dict, max_event_bytes: int) -> dict:
    """Hard-cap content against max_event_bytes. Order: drop opaque framework
    content first (arguments → result → input → output in that fixed order,
    stopping as soon as the event fits) → then drop prompt messages
    oldest-first (keeping one leading system message) until the event fits;
    if still over, drop the remaining prompts entirely (including the
    preserved system message); if still over, drop the response entirely.
    Metadata-only events that still exceed the budget are left as-is --
    that's the documented backstop (EventsWorker/API limits apply from
    there). Sets content_dropped when anything content-related was removed."""
    if _size(data) <= max_event_bytes:
        return data

    dropped_any = False

    # Opaque framework content drops first, in fixed order (spec).
    for key in ("arguments", "result", "input", "output"):
        if _size(data) <= max_event_bytes:
            break
        if key in data:
            del data[key]
            dropped_any = True

    prompts = data.get("prompts")
    if isinstance(prompts, list) and prompts:
        keep_system = prompts[0] if prompts[0].get("role") == "system" else None
        droppable = prompts[1:] if keep_system else list(prompts)
        while droppable and _size(data) > max_event_bytes:
            droppable.pop(0)
            dropped_any = True
            data["prompts"] = ([keep_system] if keep_system else []) + droppable
        if not data.get("prompts"):
            # Dropping oldest-first emptied the list (no system message to
            # preserve, or there never was one) -- drop the key entirely.
            data.pop("prompts", None)
        elif _size(data) > max_event_bytes:
            # Still over budget with only the preserved system message left:
            # drop it too. Content can no longer keep an event over budget.
            del data["prompts"]
            dropped_any = True

    if _size(data) > max_event_bytes and "response" in data:
        del data["response"]
        dropped_any = True

    if dropped_any:
        data["content_dropped"] = True
    return data
