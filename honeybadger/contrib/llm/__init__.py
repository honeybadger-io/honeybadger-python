"""Honeybadger LLM instrumentation (phase 1: OpenAI).

Spec: docs/superpowers/specs/2026-07-11-llm-instrumentation-design.md
Maintainer notes: honeybadger/contrib/llm.md
"""

import importlib.util
import logging
import os
import threading

from honeybadger import honeybadger
from . import _bridge

logger = logging.getLogger(__name__)

CONTENT_ENV_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
_EXPORT_MODES = ("events", "otlp")

# provider key -> (sdk module to detect, instrumentor module, instrumentor class)
_INSTRUMENTORS = {
    "openai": (
        "openai",
        "opentelemetry.instrumentation.genai.openai",
        "OpenAIInstrumentor",
    ),
    "anthropic": (
        "anthropic",
        "opentelemetry.instrumentation.genai.anthropic",
        "AnthropicInstrumentor",
    ),
    # BotocoreInstrumentor traces EVERY botocore call (S3, DynamoDB, ...),
    # so bedrock is explicit-only: never part of auto-detection.
    "bedrock": (
        "botocore",
        "opentelemetry.instrumentation.botocore",
        "BotocoreInstrumentor",
    ),
    # Frameworks (phase 3). 4th element = PyPI distribution name: detection
    # goes through importlib.metadata.version(dist) because the import name
    # alone is untrustworthy ("agents" is generic; langchain_core is the
    # real trigger for the langchain callback instrumentor).
    "langchain": (
        "langchain_core",
        "opentelemetry.instrumentation.genai.langchain",
        "LangChainInstrumentor",
        "langchain-core",
    ),
    "openai_agents": (
        "agents",
        "opentelemetry.instrumentation.genai.openai_agents",
        "OpenAIAgentsInstrumentor",
        "openai-agents",
    ),
}

_EXPLICIT_ONLY = frozenset({"bedrock"})
_FRAMEWORK_KEYS = frozenset({"langchain", "openai_agents"})


def _registry_entry(key):
    entry = _INSTRUMENTORS[key]
    if len(entry) == 3:
        return entry + (None,)
    return entry


def _sdk_available(key):
    """Is the instrumented SDK importable/installed? dist_name entries verify
    the installed DISTRIBUTION (import names like "agents" are claimable by
    any package); 3-tuple entries keep find_spec (no behavior change)."""
    import importlib.metadata
    import importlib.util

    sdk_module, _module, _cls, dist_name = _registry_entry(key)
    if dist_name is not None:
        try:
            importlib.metadata.version(dist_name)
            return True
        except importlib.metadata.PackageNotFoundError:
            return False
    return importlib.util.find_spec(sdk_module) is not None


_active_instance = None
_auto_instance = None
_lock = threading.Lock()
_auto_lock = threading.Lock()


def _otel_available(requested=None):
    try:
        if importlib.util.find_spec("opentelemetry.sdk") is None:
            return False
        keys = requested if requested is not None else list(_INSTRUMENTORS)
        for key in keys:
            _sdk, module_name, _cls, _dist = _registry_entry(key)
            if importlib.util.find_spec(module_name) is not None:
                return True
        return False
    except ModuleNotFoundError:
        # find_spec("opentelemetry.sdk") raises (rather than returning None)
        # when the parent "opentelemetry" package is entirely absent -- the
        # common core-only install. Treat that the same as "not found" so
        # init() raises the documented [llm]-extra ImportError instead of a
        # confusing "No module named 'opentelemetry'".
        return False


def _build_provider():
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]

    return TracerProvider()


def _attach_pipeline(self, provider):
    """Attach context processor + exporter pipeline to the provider."""
    from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
        BatchSpanProcessor,
        SimpleSpanProcessor,
    )

    provider.add_span_processor(_bridge.make_context_processor())
    exporter = self._build_exporter()
    # Lambda: batch thread may be frozen between invocations; go synchronous.
    if honeybadger.config.is_aws_lambda_environment:
        processor = SimpleSpanProcessor(exporter)
    else:
        processor = BatchSpanProcessor(exporter)
    self._processor = processor
    provider.add_span_processor(processor)


def _activate_instrumentors(self, provider):
    """Instrument each requested/detected provider we can own. Returns keys."""
    import importlib

    activated = []
    for key in self._requested_instruments():
        _sdk_module, module_name, class_name, _dist = _registry_entry(key)
        if not _sdk_available(key):
            continue
        instrumentor_cls = getattr(importlib.import_module(module_name), class_name)
        instrumentor = instrumentor_cls()
        if instrumentor.is_instrumented_by_opentelemetry:
            logger.warning(
                "honeybadger llm: %s already instrumented by another consumer; skipping",
                key,
            )
            if key in _FRAMEWORK_KEYS:
                # A foreign consumer already owns this framework's
                # instrumentor. If we activate a DIFFERENT framework and
                # borrow the same provider, that foreign framework's spans
                # can still reach our exporter -- and with only one
                # framework key in self._instrumentors, active_frameworks
                # would otherwise (wrongly) attribute them to the one we DID
                # activate. `framework` must never be guessed wrong in
                # preference to being omitted (spec), so once this is
                # detected we omit `framework` entirely for the rest of this
                # instance's lifetime.
                self._foreign_framework_detected = True
            continue
        if key in _FRAMEWORK_KEYS and not self._activated_framework:
            # Framework instrumentors share the util-genai TelemetryHandler
            # singleton; record whether one pre-exists so tearDown only
            # clears what OUR init created (see _release_genai_singleton).
            self._saw_genai_singleton = _genai_singleton_exists()
            self._activated_framework = True
            if self._saw_genai_singleton:
                logger.warning(
                    "honeybadger llm: a util-genai TelemetryHandler already "
                    "exists; framework spans may be routed to another "
                    "consumer's tracer provider, not Honeybadger's"
                )
        kwargs = dict((self.instrument_options or {}).get(key) or {})
        instrumentor.instrument(tracer_provider=provider, **kwargs)
        self._instrumentors[key] = instrumentor
        activated.append(key)
    return activated


def _deactivate_instrumentors(self):
    for key, instrumentor in list(self._instrumentors.items()):
        try:
            instrumentor.uninstrument()
        except Exception as exc:
            logger.warning("honeybadger llm: uninstrument %s failed: %s", key, exc)
        self._instrumentors.pop(key, None)


def _genai_singleton_exists():
    try:
        from opentelemetry.util.genai.handler import get_telemetry_handler
    except ImportError:
        return False
    return getattr(get_telemetry_handler, "_default_handler", None) is not None


def _release_genai_singleton(self):
    """Clear the util-genai TelemetryHandler singleton iff our init created
    it. Framework instrumentors bind the singleton's tracer to whichever
    provider existed at FIRST creation; without this, tearDown + re-init
    with only openai_agents (whose uninstrument does NOT clear it, unlike
    langchain's) would silently pipe all framework spans to the dead
    provider."""
    if not getattr(self, "_activated_framework", False):
        return
    if getattr(self, "_saw_genai_singleton", True):
        return  # pre-existing singleton is someone else's; leave it
    try:
        from opentelemetry.util.genai.handler import get_telemetry_handler
    except ImportError:
        return
    if hasattr(get_telemetry_handler, "_default_handler"):
        delattr(get_telemetry_handler, "_default_handler")


class LLMHoneybadger(object):
    def __init__(
        self,
        instruments=None,
        tracer_provider=None,
        export="events",
        instrument_options=None,
    ):
        if export not in _EXPORT_MODES:
            raise ValueError(
                "export must be one of %r, got %r" % (_EXPORT_MODES, export)
            )
        unknown = set(instrument_options or {}) - set(_INSTRUMENTORS)
        if unknown:
            # A typo'd/unknown key (e.g. "openai-agents" instead of
            # "openai_agents") would otherwise be silently ignored -- a
            # privacy foot-gun, since instrument_options is how callers
            # disable things like the Agents SDK's native trace export.
            raise ValueError("unknown instrument_options keys: %s" % sorted(unknown))
        self.instruments = instruments
        self.export = export
        self.instrument_options = instrument_options
        self._borrowed_provider = tracer_provider
        self._provider = None
        self._processor = None
        self._instrumentors = {}
        self._initialized = False
        self._env_was_set_by_us = False
        self._dedup = _bridge.ResponseDedup()
        self._saw_genai_singleton = False
        self._activated_framework = False
        self._foreign_framework_detected = False

    @property
    def active(self):
        return self._initialized

    @property
    def active_frameworks(self):
        if self._foreign_framework_detected:
            # A framework instrumentor we did NOT activate (pre-instrumented
            # by another consumer) may still be producing spans that reach
            # our exporter. Any single-framework attribution we'd otherwise
            # report could be wrong, so we omit rather than guess.
            return ()
        return tuple(k for k in self._instrumentors if k in _FRAMEWORK_KEYS)

    def _requested_instruments(self):
        if self.instruments is not None:
            unknown = set(self.instruments) - set(_INSTRUMENTORS)
            if unknown:
                raise ValueError("unknown instruments: %s" % sorted(unknown))
            return list(self.instruments)
        return [k for k in _INSTRUMENTORS if k not in _EXPLICIT_ONLY]

    def init(self):
        global _active_instance
        if self._initialized:
            return self
        with _lock:
            if self._initialized:
                return self
            if _active_instance is self:
                # Another thread is already mid-init on this exact instance.
                raise RuntimeError("init already in progress")
            if _active_instance is not None:
                raise RuntimeError(
                    "another LLMHoneybadger instance is active; tearDown() it first"
                )
            _active_instance = self
        try:
            requested = self._requested_instruments()
            if not _otel_available(requested):
                raise ImportError(
                    "LLM instrumentation requires the [llm] extra on Python >= 3.10: "
                    "pip install 'honeybadger[llm]'"
                )
            self._apply_env_gating()
            provider = self._borrowed_provider or _build_provider()
            self._provider = provider
            _attach_pipeline(self, provider)
            _activate_instrumentors(self, provider)
            self._initialized = True
        except Exception:
            self._cleanup_wiring()
            with _lock:
                if _active_instance is self:
                    _active_instance = None
            raise
        return self

    def tearDown(self):
        global _active_instance
        with _lock:
            if _active_instance is self and not self._initialized:
                # Another thread is mid-init() on this exact instance:
                # _active_instance was reserved but init() hasn't finished
                # (or failed) yet. Tearing down now would race init()'s own
                # cleanup/state transitions. Mirrors init()'s own
                # "init already in progress" guard.
                raise RuntimeError("init in progress; cannot tearDown")
            if not self._initialized and _active_instance is not self:
                return
        # Keep self.active True through _cleanup_wiring(): the owned
        # provider's final force_flush() drains any spans recorded but not
        # yet exported, and the exporter gates on owner.active (see
        # _bridge._export_one). Flipping it False first would make that
        # last flush silently drop every pending span.
        self._cleanup_wiring()
        self._initialized = False
        with _lock:
            if _active_instance is self:
                _active_instance = None

    def _apply_env_gating(self):
        # Before instrumenting: never override a user-set value.
        if CONTENT_ENV_VAR in os.environ:
            return
        llm_config = honeybadger.config.insights_config.llm
        if llm_config.include_prompts or llm_config.include_responses:
            # Verified against installed opentelemetry-util-genai source:
            # get_content_capturing_mode() (opentelemetry/util/genai/utils.py)
            # does `envvar.strip().upper()` before an Enum[] lookup on
            # ContentCapturingMode, so this is case-insensitive. Both the
            # openai and anthropic instrumentors route through that same
            # function via opentelemetry.util.genai.handler.TelemetryHandler,
            # so lowercase "span_only" enables span-content capture for both
            # -- no per-provider casing divergence despite docs showing
            # different cases (see .superpowers/sdd/task-2-report.md).
            os.environ[CONTENT_ENV_VAR] = "span_only"
            self._env_was_set_by_us = True

    def _restore_env_gating(self):
        if self._env_was_set_by_us:
            os.environ.pop(CONTENT_ENV_VAR, None)
            self._env_was_set_by_us = False

    def _build_exporter(self):
        if self.export == "otlp":
            # OTLP exporter requires the optional opentelemetry-exporter-otlp-proto-http package
            return getattr(_bridge, "make_otlp_exporter")(self)
        return _bridge.make_events_exporter(self)

    def _cleanup_wiring(self):
        _deactivate_instrumentors(self)
        _release_genai_singleton(self)
        self._restore_env_gating()
        if self._provider is not None and self._borrowed_provider is None:
            # Owned provider: flush + shutdown. Borrowed: leave attached,
            # exporter goes inert via self.active (no remove_span_processor API).
            try:
                self._provider.force_flush()
                self._provider.shutdown()
            except Exception as exc:
                logger.debug("honeybadger llm: provider shutdown failed: %s", exc)
        elif self._borrowed_provider is not None and self._processor is not None:
            # Borrowed provider: never shut IT down (it's the app's), but we
            # still need to (a) drain any spans buffered in our own
            # processor before self.active goes False, or they're silently
            # dropped, and (b) shut down OUR processor afterward so its
            # background batch-worker thread stops -- otherwise repeated
            # init/tearDown against one borrowed provider accumulates live
            # threads forever (OTel has no remove_span_processor() API, so
            # the processor stays attached, but a shutdown processor is
            # inert: on_end() becomes a no-op). The provider itself and its
            # other processors are untouched.
            try:
                self._processor.force_flush()
                self._processor.shutdown()
            except Exception as exc:
                logger.debug(
                    "honeybadger llm: processor flush/shutdown failed: %s", exc
                )
        self._provider = None
        self._processor = None
        self._dedup.clear()
        self._activated_framework = False
        self._saw_genai_singleton = False
        self._foreign_framework_detected = False


def auto_init():
    """Shared-instance init used by framework integrations. Never raises."""
    global _auto_instance
    with _auto_lock:
        try:
            if not _otel_available():
                return None
            config = honeybadger.config
            if not config.insights_enabled or config.insights_config.llm.disabled:
                return None
            if _active_instance is not None:
                return _active_instance if _active_instance is _auto_instance else None
            _auto_instance = LLMHoneybadger()
            _auto_instance.init()
            return _auto_instance
        except Exception as exc:
            logger.debug("honeybadger llm auto_init skipped: %s", exc)
            if _active_instance is not _auto_instance:
                _auto_instance = None
            return None
