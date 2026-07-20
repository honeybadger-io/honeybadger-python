"""Content policy for LLM events. Normative order (spec):
part-drop -> structural redaction -> per-string truncation -> byte budget.
All functions are pure with respect to their message inputs.
"""

import json
from typing import List, Optional

from honeybadger.utils import filter_structure

TRUNCATION_MARKER = "... [TRUNCATED]"
OMITTED_PART = "[non-text content omitted]"


def apply_content_policy(
    messages: Optional[list], filter_keys, max_content_length: int
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


def _size(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False, default=repr).encode("utf-8"))


def enforce_event_budget(data: dict, max_event_bytes: int) -> dict:
    """Drop prompt messages oldest-first (keeping one leading system message)
    until the serialized event fits. The response is preserved. Sets
    content_dropped when anything was removed."""
    if _size(data) <= max_event_bytes:
        return data

    prompts = data.get("prompts")
    if not isinstance(prompts, list) or not prompts:
        return data  # nothing droppable; EventsWorker/API limits are the backstop

    keep_system = prompts[0] if prompts and prompts[0].get("role") == "system" else None
    droppable = prompts[1:] if keep_system else list(prompts)
    dropped_any = False
    while droppable and _size(data) > max_event_bytes:
        droppable.pop(0)
        dropped_any = True
        data["prompts"] = ([keep_system] if keep_system else []) + droppable
    if dropped_any:
        data["content_dropped"] = True
    return data
