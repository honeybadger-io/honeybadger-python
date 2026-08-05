"""Fake span helpers for LLM instrumentation tests."""


class FakeSpanContext:
    def __init__(self, trace_id=0x1F, span_id=0x2):
        self.trace_id = trace_id
        self.span_id = span_id


class FakeEvent:
    def __init__(self, name, attributes=None):
        self.name = name
        self.attributes = attributes or {}


class FakeStatus:
    def __init__(self, description=None, is_ok=True):
        self.description = description
        self.is_ok = is_ok


class FakeSpan:
    """Duck-types the ReadableSpan surface the bridge reads."""

    def __init__(
        self,
        attributes=None,
        events=None,
        status=None,
        start_time=1_000_000_000,
        end_time=2_234_000_000,
        name="chat gpt-4o",
    ):
        self.attributes = attributes or {}
        self.events = events or []
        self.status = status or FakeStatus()
        self.start_time = start_time  # ns
        self.end_time = end_time  # ns
        self.name = name
        self._ctx = FakeSpanContext()

    def get_span_context(self):
        return self._ctx
