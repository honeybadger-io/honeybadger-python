"""Shared test helper: a SpanProcessor that records ended spans so
integration tests can assert raw instrumentor output (not just the emitted
event) -- see honeybadger/tests/contrib/test_llm_anthropic.py."""
from opentelemetry.sdk.trace import SpanProcessor


class RecordingProcessor(SpanProcessor):
    """Collects ended spans so integration tests can assert raw attributes."""

    def __init__(self):
        self.spans = []

    def on_end(self, span):
        self.spans.append(span)
