"""Honeybadger instrumentation for Oban (https://github.com/oban-bg/oban-py).

Reports unhandled worker exceptions to Honeybadger and emits per-job +
maintenance-loop telemetry to Honeybadger Insights.

End-user usage and configuration: see the Oban section in the project README.
Design and rationale (for maintainers): see oban.md alongside this file.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import TYPE_CHECKING, Optional

from honeybadger import honeybadger
from honeybadger.plugins import Plugin, default_plugin_manager
from honeybadger.utils import filter_dict, matches_any_pattern

if TYPE_CHECKING:
    from oban.job import Job  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

# Set by:
#   - The per-worker process wrapper (during user-code execution)
#   - The executor.wrap_result extension (during the auto-notify call)
# Read by ObanPlugin.supports/generate_payload.
_current_job_var: ContextVar[Optional["Job"]] = ContextVar(
    "honeybadger_oban_current_job", default=None
)

# Module-level single-instance guard. Telemetry handler IDs are fixed strings,
# so two live instances would create duplicate attachments and double-fire every
# Insights event. See "Single-instance guard" in oban.md.
_active_instance: Optional["ObanHoneybadger"] = None

_LOOP_EXCEPTION_EVENTS = (
    "oban.leader.election.exception",
    "oban.stager.stage.exception",
    "oban.lifeline.rescue.exception",
    "oban.pruner.prune.exception",
    "oban.refresher.refresh.exception",
    "oban.refresher.cleanup.exception",
    "oban.scheduler.evaluate.exception",
    "oban.producer.get.exception",
    "oban.producer.ack.exception",
)


def _filtered_copy(value, params_filters):
    """Filter sensitive keys (e.g. password) out of a dict before including it
    in a notice or event. Deep-copies first because filter_dict mutates nested
    dicts in place; non-dict values pass through unchanged."""
    if isinstance(value, dict):
        return filter_dict(deepcopy(value), params_filters, remove_keys=True)
    return value


@contextmanager
def _event_context_from_meta(job):
    """Temporarily merge propagated event context from job.meta, if any.

    Uses honeybadger.event_context (token-based ContextVar) so any pre-existing
    event context is restored on exit, not cleared.
    """
    if not isinstance(getattr(job, "meta", None), dict):
        yield
        return
    ctx = job.meta.get("honeybadger_event_context")
    if not isinstance(ctx, dict):
        yield
        return
    with honeybadger.event_context(ctx):
        yield


class ObanPlugin(Plugin):
    def __init__(self):
        super().__init__("Oban")

    def supports(self, config, context):
        return _current_job_var.get() is not None

    def generate_payload(self, default_payload, config, context):
        job = _current_job_var.get()
        if job is None:
            return default_payload

        worker_name = job.worker  # "module.qualname" string from oban
        if "." in worker_name:
            module = worker_name.rsplit(".", 1)[0]
        else:
            module = worker_name

        merged_context = dict(context or {})
        merged_context.update(
            job_id=job.id,
            queue=job.queue,
            attempt=job.attempt,
            max_attempts=job.max_attempts,
        )

        # Job tags become fault tags via the real tags channel (error.tags).
        # Never put them in context: the server treats context["tags"] as a
        # reserved key holding a comma-separated string, and stringifies a
        # list into junk tags like "[]" or '["alpha"'.
        if job.tags:
            error_tags = default_payload.setdefault("error", {}).setdefault("tags", [])
            error_tags.extend(
                t for t in job.tags if isinstance(t, str) and t not in error_tags
            )

        params_filters = honeybadger.config.params_filters
        default_payload["request"].update(
            {
                "component": module,
                "action": worker_name,
                "params": {
                    "args": _filtered_copy(job.args, params_filters),
                    "meta": _filtered_copy(job.meta, params_filters),
                },
                "context": merged_context,
            }
        )
        return default_payload


class ObanHoneybadger:
    """Wires Honeybadger into Oban.

    Usage:

        ObanHoneybadger(report_exceptions=True).init()

    Only one instance may be active per process at a time.
    """

    def __init__(self, report_exceptions: bool = False):
        self.report_exceptions = report_exceptions
        self._initialized = False
        # Per-target state, initialized so that cleanup after a partial-init
        # failure can safely inspect every flag without AttributeError.
        self._wrapped_classes: list = []
        self._prev_after_register = None
        self._after_register_chain_installed = None
        self._patched_enqueue_many = False
        self._prev_wrap_result = None
        self._wrap_result_extension = None
        self._wrap_result_installed = False
        self._job_telemetry_attached = False
        self._loop_telemetry_attached = False

    def init(self):
        """Wire up extensions, telemetry, and worker wrappers. Idempotent."""
        if self._initialized:
            return  # same-instance early return; see oban.md

        global _active_instance
        if _active_instance is not None and _active_instance is not self:
            raise RuntimeError(
                "ObanHoneybadger already initialized; "
                "call tearDown() on the previous instance first"
            )
        _active_instance = self

        try:
            self._perform_init()
        except BaseException:
            # Partial init: best-effort revert anything we wired up so far,
            # then clear the single-instance lock so the caller can retry
            # (or another instance can init) once the underlying issue is fixed.
            try:
                self._cleanup_wiring()
            finally:
                _active_instance = None
            raise

        self._initialized = True

    def _perform_init(self):
        # Register the plugin (idempotent — PluginManager keys by name).
        default_plugin_manager.register(ObanPlugin())

        # Imported lazily so the module file imports cleanly without oban installed.
        from oban._extensions import get_ext, put_ext
        from oban.worker import _registry

        # Wrap every already-registered worker class.
        for cls in list(_registry.values()):
            self._wrap_worker_class(cls)

        # Chain into worker.after_register so future workers are wrapped too.
        self._prev_after_register = get_ext("worker.after_register", lambda _cls: None)
        self._after_register_chain_installed = self._after_register_chain
        put_ext("worker.after_register", self._after_register_chain_installed)

        # Patch Oban.enqueue_many for write-side event-context propagation.
        from oban import Oban
        from oban.job import Job

        if not getattr(Oban.enqueue_many, "_honeybadger_patched", False):
            original_enqueue_many = Oban.enqueue_many

            async def _wrapped_enqueue_many(
                self_inner, jobs_or_first, /, *rest, conn=None
            ):
                ctx = honeybadger._get_event_context()
                if ctx:
                    if isinstance(jobs_or_first, Job):
                        jobs_iter = [jobs_or_first, *rest]
                    else:
                        jobs_iter = list(jobs_or_first)
                        # We've consumed the iterable; pass it back as a list.
                        jobs_or_first = jobs_iter
                        rest = ()
                    for job in jobs_iter:
                        base = job.meta if isinstance(job.meta, dict) else {}
                        job.meta = {**base, "honeybadger_event_context": dict(ctx)}
                return await original_enqueue_many(
                    self_inner, jobs_or_first, *rest, conn=conn
                )

            _wrapped_enqueue_many._honeybadger_patched = True
            _wrapped_enqueue_many._honeybadger_original = original_enqueue_many
            Oban.enqueue_many = _wrapped_enqueue_many
            self._patched_enqueue_many = True
        else:
            self._patched_enqueue_many = False

        # executor.wrap_result extension for error reporting.
        if self.report_exceptions:
            self._prev_wrap_result = get_ext(
                "executor.wrap_result", lambda _job, result: result
            )
            self._wrap_result_extension = self._make_wrap_result_extension()
            put_ext("executor.wrap_result", self._wrap_result_extension)
            self._wrap_result_installed = True
        else:
            self._wrap_result_installed = False

        # Insights telemetry handlers.
        insights_enabled = (
            honeybadger.config.insights_enabled
            and not honeybadger.config.insights_config.oban.disabled
        )
        if insights_enabled:
            from oban import telemetry

            telemetry.attach(
                "honeybadger-oban-jobs",
                ["oban.job.stop", "oban.job.exception"],
                self._on_job_event,
            )
            self._job_telemetry_attached = True
            telemetry.attach(
                "honeybadger-oban-loops",
                list(_LOOP_EXCEPTION_EVENTS),
                self._on_loop_exception,
            )
            self._loop_telemetry_attached = True
        else:
            self._job_telemetry_attached = False
            self._loop_telemetry_attached = False

    def _wrap_worker_class(self, cls):
        if getattr(cls, "_honeybadger_process_wrapped", False):
            return
        original = cls.process
        cls._honeybadger_original_process = original
        cls.process = self._make_wrapped_process(original)
        cls._honeybadger_process_wrapped = True
        self._wrapped_classes.append(cls)

    def _make_wrapped_process(self, original):
        async def _wrapped_process(worker_self, job):
            job_token = _current_job_var.set(job)
            try:
                with _event_context_from_meta(job):
                    return await original(worker_self, job)
            finally:
                _current_job_var.reset(job_token)

        return _wrapped_process

    def _after_register_chain(self, cls):
        # Call the prior extension first (the decorator's default is a no-op).
        # _prev_after_register may be None after _cleanup_wiring() runs, if a
        # later integration captured this method via the chain pattern and
        # keeps calling it post-teardown.
        if self._prev_after_register is not None:
            self._prev_after_register(cls)
        if not self._initialized:
            return  # torn down; do not wrap further workers
        self._wrap_worker_class(cls)

    def _make_wrap_result_extension(self):
        prev = self._prev_wrap_result
        hb_oban = self  # closure ref so we can check liveness after teardown

        def _wrap_result(job, result):
            # Chain prior extension first; let its exceptions propagate so we
            # preserve prior Oban behavior bit-for-bit.
            result = prev(job, result)

            if not hb_oban._initialized:
                # Honeybadger was torn down; remain inert but still chain prev so
                # later integrations that captured us by reference don't break.
                return result

            if isinstance(result, Exception):
                job_token = _current_job_var.set(job)
                try:
                    with _event_context_from_meta(job):
                        honeybadger.notify(exception=result)
                except Exception:
                    logger.exception("Failed to report Oban exception to Honeybadger")
                finally:
                    _current_job_var.reset(job_token)
            return result

        return _wrap_result

    def _on_job_event(self, name, meta):
        try:
            if not self._insights_active():
                return

            job = meta["job"]
            oban_cfg = honeybadger.config.insights_config.oban

            if self._is_worker_excluded(job.worker, oban_cfg.exclude_workers):
                return

            payload = {
                "job_id": job.id,
                "worker": job.worker,
                "queue": job.queue,
                "state": meta.get("state"),
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
                "duration": meta.get("duration", 0) / 1_000_000,
                "queue_time": meta.get("queue_time", 0) / 1_000_000,
                "tags": job.tags,
            }
            if name == "oban.job.exception":
                payload["error_type"] = meta.get("error_type")
                payload["error_message"] = meta.get("error_message")

            if oban_cfg.include_args:
                params_filters = honeybadger.config.params_filters
                payload["args"] = _filtered_copy(job.args, params_filters)
                payload["meta"] = _filtered_copy(
                    job.meta if isinstance(job.meta, dict) else {}, params_filters
                )

            with _event_context_from_meta(job):
                honeybadger.event("oban.job_finished", payload)
        except Exception:
            logger.exception("Failed to emit Oban Insights event for %s", name)

    def _on_loop_exception(self, name, meta):
        try:
            if not self._insights_active():
                return

            # name == "oban.<loop>.<action>.exception"
            parts = name.split(".")
            if len(parts) < 4:
                return
            loop = parts[1]
            honeybadger.event(
                f"oban.{loop}_exception",
                {
                    "loop": loop,
                    "event": name,
                    "error_type": meta.get("error_type"),
                    "error_message": meta.get("error_message"),
                    "duration": meta.get("duration", 0) / 1_000_000,
                },
            )
        except Exception:
            logger.exception("Failed to emit Oban loop-exception event for %s", name)

    @staticmethod
    def _insights_active():
        # Re-check at emit time, not just at attach time: config is mutable, so
        # a user can flip insights_enabled or oban.disabled after init() without
        # tearing down. Guards both telemetry handlers.
        config = honeybadger.config
        return config.insights_enabled and not config.insights_config.oban.disabled

    @staticmethod
    def _is_worker_excluded(worker_name, patterns):
        return matches_any_pattern(worker_name, patterns)

    def tearDown(self):
        """Reverse all wiring done by init(). Idempotent."""
        global _active_instance
        if _active_instance is not self:
            return
        try:
            self._cleanup_wiring()
        finally:
            self._initialized = False
            _active_instance = None

    def _cleanup_wiring(self):
        """Best-effort revert of every per-target wiring step.

        Safe to call after a partial init() failure as well as from tearDown().
        Each step is gated by its own flag so absent state is a no-op.
        """
        # Lazy imports: cleanup may run after a failed init where oban itself
        # wasn't importable, but in that case nothing got wired and every flag
        # is its initial value, so all per-target blocks below are no-ops and
        # we never reach the imports.
        if self._patched_enqueue_many:
            from oban import Oban

            if getattr(Oban.enqueue_many, "_honeybadger_patched", False):
                Oban.enqueue_many = Oban.enqueue_many._honeybadger_original
            self._patched_enqueue_many = False

        if self._job_telemetry_attached:
            from oban import telemetry

            telemetry.detach("honeybadger-oban-jobs")
            self._job_telemetry_attached = False

        if self._loop_telemetry_attached:
            from oban import telemetry

            telemetry.detach("honeybadger-oban-loops")
            self._loop_telemetry_attached = False

        if self._wrap_result_installed:
            from oban._extensions import get_ext, put_ext

            current = get_ext("executor.wrap_result", None)
            if current is self._wrap_result_extension:
                put_ext("executor.wrap_result", self._prev_wrap_result)
            # else: a later integration replaced our extension; leave it alone.
            self._wrap_result_installed = False

        if self._after_register_chain_installed is not None:
            from oban._extensions import get_ext, put_ext

            current = get_ext("worker.after_register", None)
            if current is self._after_register_chain_installed:
                put_ext("worker.after_register", self._prev_after_register)
            self._after_register_chain_installed = None
            self._prev_after_register = None

        # Unwrap every worker class we wrapped.
        for cls in self._wrapped_classes:
            if getattr(cls, "_honeybadger_process_wrapped", False):
                cls.process = cls._honeybadger_original_process
                del cls._honeybadger_original_process
                del cls._honeybadger_process_wrapped
        self._wrapped_classes = []
