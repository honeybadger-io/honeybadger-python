import sys
from unittest.mock import patch
import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="oban requires Python 3.12+"
)


def test_plugin_supports_returns_false_outside_job():
    from honeybadger.contrib.oban import ObanPlugin, _current_job_var

    assert _current_job_var.get() is None
    plugin = ObanPlugin()
    assert plugin.supports({}, {}) is False


def test_plugin_supports_returns_true_when_current_job_set():
    from honeybadger.contrib.oban import ObanPlugin, _current_job_var
    from unittest.mock import MagicMock

    job = MagicMock()
    token = _current_job_var.set(job)
    try:
        plugin = ObanPlugin()
        assert plugin.supports({}, {}) is True
    finally:
        _current_job_var.reset(token)


def test_plugin_generate_payload_enriches_with_job_fields():
    from honeybadger.contrib.oban import ObanPlugin, _current_job_var
    from unittest.mock import MagicMock

    job = MagicMock()
    job.id = 42
    job.worker = "myapp.workers.SendEmail"
    job.queue = "mailers"
    job.attempt = 2
    job.max_attempts = 5
    job.args = {"to": "user@example.com"}
    job.meta = {"foo": "bar"}
    job.tags = ["email", "transactional"]

    token = _current_job_var.set(job)
    try:
        plugin = ObanPlugin()
        payload = plugin.generate_payload({"request": {}}, {}, {})
    finally:
        _current_job_var.reset(token)

    request = payload["request"]
    assert request["component"] == "myapp.workers"
    assert request["action"] == "myapp.workers.SendEmail"
    assert request["params"]["args"] == {"to": "user@example.com"}
    assert request["params"]["meta"] == {"foo": "bar"}
    assert request["context"]["job_id"] == 42
    assert request["context"]["queue"] == "mailers"
    assert request["context"]["attempt"] == 2
    assert request["context"]["max_attempts"] == 5
    # Tags must flow through error.tags (real fault tags), never context["tags"]:
    # the server treats that context key as a comma-separated string and
    # stringifies lists into junk tags like "[]".
    assert "tags" not in request["context"]
    assert payload["error"]["tags"] == ["email", "transactional"]


def test_plugin_generate_payload_merges_job_tags_into_existing_error_tags():
    from honeybadger.contrib.oban import ObanPlugin, _current_job_var
    from unittest.mock import MagicMock

    job = MagicMock()
    job.id = 1
    job.worker = "MyW"
    job.queue = "default"
    job.attempt = 1
    job.max_attempts = 5
    job.args = {}
    job.meta = {}
    job.tags = ["email", "urgent"]

    token = _current_job_var.set(job)
    try:
        plugin = ObanPlugin()
        payload = plugin.generate_payload(
            {"request": {}, "error": {"tags": ["urgent"]}}, {}, {}
        )
    finally:
        _current_job_var.reset(token)

    # notify()-provided tags are kept; job tags appended without duplicates.
    assert payload["error"]["tags"] == ["urgent", "email"]


def test_plugin_generate_payload_omits_tags_when_job_has_none():
    from honeybadger.contrib.oban import ObanPlugin, _current_job_var
    from unittest.mock import MagicMock

    job = MagicMock()
    job.id = 1
    job.worker = "MyW"
    job.queue = "default"
    job.attempt = 1
    job.max_attempts = 5
    job.args = {}
    job.meta = {}
    job.tags = []

    token = _current_job_var.set(job)
    try:
        plugin = ObanPlugin()
        payload = plugin.generate_payload({"request": {}}, {}, {})
    finally:
        _current_job_var.reset(token)

    assert "tags" not in payload["request"]["context"]
    assert "error" not in payload  # nothing injected for tagless jobs


def test_event_context_from_meta_is_noop_when_no_context():
    from honeybadger.contrib.oban import _event_context_from_meta
    from unittest.mock import MagicMock

    job = MagicMock()
    job.meta = {}
    # Should not raise; should be a usable context manager that yields.
    with _event_context_from_meta(job):
        pass


def test_event_context_from_meta_sets_context_when_present():
    from honeybadger import honeybadger
    from honeybadger.contrib.oban import _event_context_from_meta
    from unittest.mock import MagicMock

    job = MagicMock()
    job.meta = {"honeybadger_event_context": {"request_id": "abc-123"}}
    # Snapshot before
    before = honeybadger._get_event_context()
    with _event_context_from_meta(job):
        assert honeybadger._get_event_context().get("request_id") == "abc-123"
    # After block, original context is restored
    assert honeybadger._get_event_context() == before


def test_event_context_from_meta_ignores_non_dict_meta():
    from honeybadger.contrib.oban import _event_context_from_meta
    from unittest.mock import MagicMock

    job = MagicMock()
    job.meta = None
    with _event_context_from_meta(job):
        pass  # must not raise


@pytest.fixture
def reset_oban_registry():
    """Clear the worker registry, our active-instance state, and any leaked
    Honeybadger context before AND after each test. Tests assert on the
    "no context set" state, which requires defending against pollution from
    unrelated tests in the same process run.
    """
    from oban.worker import _registry
    from oban._extensions import _extensions
    from honeybadger import honeybadger as hb_module
    import honeybadger.contrib.oban as hb_oban

    def _reset():
        _registry.clear()
        _extensions.clear()
        hb_oban._active_instance = None
        hb_module.reset_context()
        hb_module.reset_event_context()

    _reset()
    yield
    _reset()


def test_construction_without_init_does_not_register_plugin(reset_oban_registry):
    from honeybadger.plugins import default_plugin_manager
    from honeybadger.contrib.oban import ObanHoneybadger

    # Snapshot before
    had_oban = "Oban" in default_plugin_manager._registered

    ObanHoneybadger()  # construction only, no init

    if not had_oban:
        # Construction alone should not have added the plugin.
        assert "Oban" not in default_plugin_manager._registered


@pytest.mark.asyncio
async def test_init_wraps_existing_worker_process(reset_oban_registry):
    from oban import worker
    from honeybadger.contrib.oban import ObanHoneybadger, _current_job_var

    captured = {}

    @worker(queue="default")
    class W:
        async def process(self, job):
            captured["job_in_process"] = _current_job_var.get()
            return None

    hb = ObanHoneybadger()
    hb.init()
    try:
        from oban.job import Job

        job = Job("W", args={})
        instance = W()
        await instance.process(job)
        assert captured["job_in_process"] is job
        # After process returns, the ContextVar must be reset.
        assert _current_job_var.get() is None
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_init_wraps_worker_registered_after_init(reset_oban_registry):
    from oban import worker
    from honeybadger.contrib.oban import ObanHoneybadger, _current_job_var

    hb = ObanHoneybadger()
    hb.init()
    try:
        captured = {}

        @worker(queue="default")
        class LateW:
            async def process(self, job):
                captured["job_in_process"] = _current_job_var.get()
                return None

        from oban.job import Job

        job = Job("LateW", args={})
        await LateW().process(job)
        assert captured["job_in_process"] is job
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_wrapped_process_applies_event_context_from_meta(reset_oban_registry):
    from oban import worker
    from honeybadger import honeybadger as hb_module
    from honeybadger.contrib.oban import ObanHoneybadger

    captured = {}

    @worker(queue="default")
    class CtxW:
        async def process(self, job):
            captured["ctx_during"] = hb_module._get_event_context().copy()
            return None

    hb = ObanHoneybadger()
    hb.init()
    try:
        from oban.job import Job

        job = Job(
            "CtxW", args={}, meta={"honeybadger_event_context": {"request_id": "r-1"}}
        )
        await CtxW().process(job)
        assert captured["ctx_during"].get("request_id") == "r-1"
        # After process, event context is restored.
        assert hb_module._get_event_context().get("request_id") is None
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_teardown_unwraps_worker_process(reset_oban_registry):
    from oban import worker
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class W2:
        async def process(self, job):
            return None

    original_process = W2.process

    hb = ObanHoneybadger()
    hb.init()
    assert W2.process is not original_process  # wrapped

    hb.tearDown()
    assert W2.process is original_process  # restored


@pytest.mark.asyncio
async def test_enqueue_many_injects_event_context_when_set(reset_oban_registry):
    from oban import Oban, worker
    from oban.job import Job
    from honeybadger import honeybadger as hb_module
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class EW:
        async def process(self, job):
            return None

    # Stand up an Oban instance without a real pool by mocking the insert.
    from unittest.mock import AsyncMock, MagicMock

    oban_inst = Oban.__new__(Oban)
    oban_inst._query = MagicMock()
    oban_inst._query.insert_jobs = AsyncMock(side_effect=lambda jobs, conn=None: jobs)
    oban_inst._producers = {}
    oban_inst._name = "Oban"
    Oban._instances = {"Oban": oban_inst}  # for any code that calls get_instance

    hb = ObanHoneybadger()
    hb.init()
    try:
        hb_module.set_event_context({"request_id": "req-9"})
        try:
            job = Job("EW", args={"x": 1})
            result = await oban_inst.enqueue_many([job])
        finally:
            hb_module.reset_event_context()

        assert result[0].meta.get("honeybadger_event_context") == {
            "request_id": "req-9"
        }
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_enqueue_many_leaves_meta_unchanged_without_event_context(
    reset_oban_registry,
):
    from oban import Oban, worker
    from oban.job import Job
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class EW2:
        async def process(self, job):
            return None

    from unittest.mock import AsyncMock, MagicMock

    oban_inst = Oban.__new__(Oban)
    oban_inst._query = MagicMock()
    oban_inst._query.insert_jobs = AsyncMock(side_effect=lambda jobs, conn=None: jobs)
    oban_inst._producers = {}
    oban_inst._name = "Oban"
    Oban._instances = {"Oban": oban_inst}

    hb = ObanHoneybadger()
    hb.init()
    try:
        job = Job("EW2", args={"x": 2}, meta={"custom": "value"})
        result = await oban_inst.enqueue_many([job])
        # Original meta preserved; no injection happened.
        assert result[0].meta == {"custom": "value"}
        assert "honeybadger_event_context" not in result[0].meta
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_enqueue_many_handles_variadic_form(reset_oban_registry):
    from oban import Oban, worker
    from oban.job import Job
    from honeybadger import honeybadger as hb_module
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class EW3:
        async def process(self, job):
            return None

    from unittest.mock import AsyncMock, MagicMock

    oban_inst = Oban.__new__(Oban)
    oban_inst._query = MagicMock()
    oban_inst._query.insert_jobs = AsyncMock(side_effect=lambda jobs, conn=None: jobs)
    oban_inst._producers = {}
    oban_inst._name = "Oban"
    Oban._instances = {"Oban": oban_inst}

    hb = ObanHoneybadger()
    hb.init()
    try:
        hb_module.set_event_context({"request_id": "req-multi"})
        try:
            j1 = Job("EW3", args={"x": 1})
            j2 = Job("EW3", args={"x": 2})
            result = await oban_inst.enqueue_many(j1, j2)
        finally:
            hb_module.reset_event_context()

        assert len(result) == 2
        for j in result:
            assert j.meta.get("honeybadger_event_context") == {
                "request_id": "req-multi"
            }
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_teardown_restores_original_enqueue_many(reset_oban_registry):
    from oban import Oban
    from honeybadger.contrib.oban import ObanHoneybadger

    original = Oban.enqueue_many
    hb = ObanHoneybadger()
    hb.init()
    assert Oban.enqueue_many is not original
    hb.tearDown()
    assert Oban.enqueue_many is original


@pytest.mark.asyncio
async def test_wrap_result_reports_exception_for_worker_class(reset_oban_registry):
    from oban import worker
    from oban.job import Job
    from oban._executor import Executor
    from oban.worker import worker_name
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class FailW:
        async def process(self, job):
            raise ValueError("boom")

    hb = ObanHoneybadger(report_exceptions=True)
    hb.init()
    try:
        # Use the fully-qualified registry name so Executor.resolve_worker
        # can actually import this class instead of failing with
        # ValueError("Empty module name") on a bare label.
        job = Job(worker_name(FailW), args={}, id=1, attempt=1)
        with patch("honeybadger.contrib.oban.honeybadger.notify") as notify:
            await Executor(job).execute()
        assert notify.call_count == 1
        assert isinstance(notify.call_args[1]["exception"], ValueError)
        assert str(notify.call_args[1]["exception"]) == "boom"
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_wrap_result_reports_exception_for_job_function(reset_oban_registry):
    from oban import job as job_decorator
    from oban.job import Job
    from oban._executor import Executor
    from honeybadger.contrib.oban import ObanHoneybadger

    @job_decorator(queue="default")
    def send_email(to: str):
        raise RuntimeError("nope")

    hb = ObanHoneybadger(report_exceptions=True)
    hb.init()
    try:
        # @job builds a FunctionWorker registered under the function's qualname.
        from oban.worker import _registry

        worker_name = next(iter(_registry))
        job_inst = Job(worker_name, args={"to": "x@example.com"}, id=2, attempt=1)
        with patch("honeybadger.contrib.oban.honeybadger.notify") as notify:
            await Executor(job_inst).execute()
        assert notify.call_count == 1
        assert isinstance(notify.call_args[1]["exception"], RuntimeError)
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_no_notify_when_report_exceptions_false(reset_oban_registry):
    from oban import worker
    from oban.job import Job
    from oban._executor import Executor
    from oban.worker import worker_name
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class FailW2:
        async def process(self, job):
            raise ValueError("boom")

    hb = ObanHoneybadger(report_exceptions=False)
    hb.init()
    try:
        job = Job(worker_name(FailW2), args={}, id=3, attempt=1)
        with patch("honeybadger.contrib.oban.honeybadger.notify") as notify:
            await Executor(job).execute()
        assert notify.call_count == 0
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_wrap_result_chains_prior_extension(reset_oban_registry):
    from oban import worker
    from oban.job import Job
    from oban._executor import Executor
    from oban._extensions import put_ext
    from oban.worker import worker_name
    from honeybadger.contrib.oban import ObanHoneybadger

    prior_calls = []

    def prior_ext(j, result):
        prior_calls.append((j.id, type(result).__name__))
        return result

    put_ext("executor.wrap_result", prior_ext)

    @worker(queue="default")
    class FailW3:
        async def process(self, job):
            raise ValueError("boom")

    hb = ObanHoneybadger(report_exceptions=True)
    hb.init()
    try:
        job = Job(worker_name(FailW3), args={}, id=4, attempt=1)
        with patch("honeybadger.contrib.oban.honeybadger.notify"):
            await Executor(job).execute()
        assert prior_calls == [(4, "ValueError")]
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_exclude_workers_does_not_filter_errors(reset_oban_registry):
    """exclude_workers filters Insights events only, not error reporting."""
    from oban import worker
    from oban.job import Job
    from oban._executor import Executor
    from oban.worker import worker_name
    from honeybadger import honeybadger as hb_module
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class ExcludedW:
        async def process(self, job):
            raise ValueError("still reported")

    # Configure to exclude this worker from Insights.
    hb_module.configure(insights_enabled=True)
    hb_module.config.insights_config.oban.exclude_workers = [worker_name(ExcludedW)]

    hb = ObanHoneybadger(report_exceptions=True)
    hb.init()
    try:
        job = Job(worker_name(ExcludedW), args={}, id=5, attempt=1)
        with patch("honeybadger.contrib.oban.honeybadger.notify") as notify:
            await Executor(job).execute()
        # Error path is unaffected by exclude_workers.
        assert notify.call_count == 1
    finally:
        hb.tearDown()
        from honeybadger.config import Configuration

        hb_module.config = Configuration()


@pytest.fixture
def with_insights():
    from honeybadger import honeybadger as hb_module
    from honeybadger.config import Configuration

    hb_module.configure(insights_enabled=True, force_report_data=True)
    yield hb_module
    hb_module.config = Configuration()


@pytest.mark.asyncio
async def test_telemetry_stop_emits_insights_event(reset_oban_registry, with_insights):
    from oban import worker, telemetry
    from oban.job import Job
    from honeybadger.contrib.oban import ObanHoneybadger
    from honeybadger.utils import filter_dict  # noqa: F401  (ensures import works)

    @worker(queue="default")
    class StopW:
        async def process(self, job):
            return None

    hb = ObanHoneybadger()
    hb.init()
    try:
        job = Job(
            "StopW",
            args={"x": 1},
            id=10,
            queue="default",
            attempt=1,
            max_attempts=5,
            meta={},
            tags=[],
        )
        with patch("honeybadger.contrib.oban.honeybadger.event") as event:
            telemetry.execute(
                "oban.job.stop",
                {
                    "job": job,
                    "state": "completed",
                    "duration": 5_000_000,  # 5ms in ns
                    "queue_time": 1_000_000,  # 1ms in ns
                    "monotonic_time": 0,
                },
            )
        assert event.call_count == 1
        name, payload = event.call_args[0]
        assert name == "oban.job_finished"
        assert payload["job_id"] == 10
        assert payload["worker"] == "StopW"
        assert payload["state"] == "completed"
        assert payload["duration"] == 5.0
        assert payload["queue_time"] == 1.0
        # include_args defaults to False — no args/meta in payload.
        assert "args" not in payload
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_telemetry_exception_includes_error_fields(
    reset_oban_registry, with_insights
):
    from oban import worker, telemetry
    from oban.job import Job
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class ExcW:
        async def process(self, job):
            return None

    hb = ObanHoneybadger()
    hb.init()
    try:
        job = Job(
            "ExcW",
            args={},
            id=11,
            queue="default",
            attempt=2,
            max_attempts=5,
            meta={},
            tags=[],
        )
        with patch("honeybadger.contrib.oban.honeybadger.event") as event:
            telemetry.execute(
                "oban.job.exception",
                {
                    "job": job,
                    "state": "retryable",
                    "duration": 3_000_000,
                    "queue_time": 0,
                    "monotonic_time": 0,
                    "error_type": "ValueError",
                    "error_message": "boom",
                    "traceback": "fake traceback",
                },
            )
        assert event.call_count == 1
        _name, payload = event.call_args[0]
        assert payload["state"] == "retryable"
        assert payload["error_type"] == "ValueError"
        assert payload["error_message"] == "boom"
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_exclude_workers_filters_insights_events(
    reset_oban_registry, with_insights
):
    from oban import worker, telemetry
    from oban.job import Job
    from honeybadger import honeybadger as hb_module
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class NoisyW:
        async def process(self, job):
            return None

    hb_module.config.insights_config.oban.exclude_workers = ["NoisyW"]

    hb = ObanHoneybadger()
    hb.init()
    try:
        job = Job(
            "NoisyW",
            args={},
            id=12,
            queue="default",
            attempt=1,
            max_attempts=5,
            meta={},
            tags=[],
        )
        with patch("honeybadger.contrib.oban.honeybadger.event") as event:
            telemetry.execute(
                "oban.job.stop",
                {
                    "job": job,
                    "state": "completed",
                    "duration": 0,
                    "queue_time": 0,
                    "monotonic_time": 0,
                },
            )
        assert event.call_count == 0
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_job_event_skipped_when_insights_disabled_at_runtime(
    reset_oban_registry, with_insights
):
    """Config is mutable: flipping oban.disabled after init() must stop events
    even though the telemetry handler is still attached."""
    from oban import worker, telemetry
    from oban.job import Job
    from honeybadger import honeybadger as hb_module
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class RuntimeOffW:
        async def process(self, job):
            return None

    hb = ObanHoneybadger()
    hb.init()
    try:
        # Disable AFTER init (handler is already attached).
        hb_module.config.insights_config.oban.disabled = True

        job = Job(
            "RuntimeOffW",
            args={},
            id=99,
            queue="default",
            attempt=1,
            max_attempts=5,
            meta={},
            tags=[],
        )
        with patch("honeybadger.contrib.oban.honeybadger.event") as event:
            telemetry.execute(
                "oban.job.stop",
                {
                    "job": job,
                    "state": "completed",
                    "duration": 0,
                    "queue_time": 0,
                    "monotonic_time": 0,
                },
            )
            telemetry.execute(
                "oban.scheduler.evaluate.exception",
                {
                    "monotonic_time": 0,
                    "duration": 0,
                    "error_type": "E",
                    "error_message": "m",
                    "traceback": "t",
                },
            )
        assert event.call_count == 0
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_include_args_filters_sensitive_keys(reset_oban_registry, with_insights):
    from oban import worker, telemetry
    from oban.job import Job
    from honeybadger import honeybadger as hb_module
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class ArgsW:
        async def process(self, job):
            return None

    hb_module.config.insights_config.oban.include_args = True

    hb = ObanHoneybadger()
    hb.init()
    try:
        job = Job(
            "ArgsW",
            args={"user_id": 7, "password": "secret"},
            id=13,
            queue="default",
            attempt=1,
            max_attempts=5,
            meta={"token": "xyz", "ok": True},
            tags=[],
        )
        with patch("honeybadger.contrib.oban.honeybadger.event") as event:
            telemetry.execute(
                "oban.job.stop",
                {
                    "job": job,
                    "state": "completed",
                    "duration": 0,
                    "queue_time": 0,
                    "monotonic_time": 0,
                },
            )
        _name, payload = event.call_args[0]
        assert "password" not in payload["args"]
        assert payload["args"]["user_id"] == 7
        # meta is not filtered by default params_filters (only "password" etc.),
        # so "token" passes through unless params_filters configured for it.
        assert "ok" in payload["meta"]
        # filter_dict must NOT mutate the live job's dicts.
        assert job.args == {"user_id": 7, "password": "secret"}
        assert job.meta == {"token": "xyz", "ok": True}
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_telemetry_handler_applies_propagated_event_context(
    reset_oban_registry, with_insights
):
    from oban import worker, telemetry
    from oban.job import Job
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class CtxTW:
        async def process(self, job):
            return None

    hb = ObanHoneybadger()
    hb.init()
    try:
        job = Job(
            "CtxTW",
            args={},
            id=14,
            queue="default",
            attempt=1,
            max_attempts=5,
            meta={"honeybadger_event_context": {"request_id": "ctx-job"}},
            tags=[],
        )

        captured = {}

        def fake_event(name, payload):
            from honeybadger import honeybadger as hb_module

            captured["ctx_at_event_time"] = hb_module._get_event_context().copy()

        with patch(
            "honeybadger.contrib.oban.honeybadger.event", side_effect=fake_event
        ):
            telemetry.execute(
                "oban.job.stop",
                {
                    "job": job,
                    "state": "completed",
                    "duration": 0,
                    "queue_time": 0,
                    "monotonic_time": 0,
                },
            )

        assert captured["ctx_at_event_time"].get("request_id") == "ctx-job"
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_scheduler_exception_emits_insights_event(
    reset_oban_registry, with_insights
):
    from oban import telemetry
    from honeybadger.contrib.oban import ObanHoneybadger

    hb = ObanHoneybadger()
    hb.init()
    try:
        with patch("honeybadger.contrib.oban.honeybadger.event") as event:
            telemetry.execute(
                "oban.scheduler.evaluate.exception",
                {
                    "monotonic_time": 0,
                    "duration": 2_500_000,  # 2.5ms
                    "error_type": "RuntimeError",
                    "error_message": "scheduler tick failed",
                    "traceback": "fake tb",
                },
            )
        assert event.call_count == 1
        name, payload = event.call_args[0]
        assert name == "oban.scheduler_exception"
        assert payload["loop"] == "scheduler"
        assert payload["event"] == "oban.scheduler.evaluate.exception"
        assert payload["error_type"] == "RuntimeError"
        assert payload["error_message"] == "scheduler tick failed"
        assert payload["duration"] == 2.5
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_all_loop_exception_events_are_attached(
    reset_oban_registry, with_insights
):
    from oban import telemetry
    from honeybadger.contrib.oban import ObanHoneybadger

    expected = [
        ("oban.leader.election.exception", "leader"),
        ("oban.stager.stage.exception", "stager"),
        ("oban.lifeline.rescue.exception", "lifeline"),
        ("oban.pruner.prune.exception", "pruner"),
        ("oban.refresher.refresh.exception", "refresher"),
        ("oban.refresher.cleanup.exception", "refresher"),
        ("oban.scheduler.evaluate.exception", "scheduler"),
        ("oban.producer.get.exception", "producer"),
        ("oban.producer.ack.exception", "producer"),
    ]

    hb = ObanHoneybadger()
    hb.init()
    try:
        for evt_name, loop in expected:
            with patch("honeybadger.contrib.oban.honeybadger.event") as event:
                telemetry.execute(
                    evt_name,
                    {
                        "monotonic_time": 0,
                        "duration": 0,
                        "error_type": "E",
                        "error_message": "m",
                        "traceback": "t",
                    },
                )
            assert event.call_count == 1, f"no event for {evt_name}"
            name, payload = event.call_args[0]
            assert name == f"oban.{loop}_exception"
            assert payload["loop"] == loop
            assert payload["event"] == evt_name
    finally:
        hb.tearDown()


def test_loop_exception_events_exist_in_oban():
    """Every subscribed loop event must match a telemetry.span prefix that the
    installed oban version actually emits — guards against subscribing to
    renamed or nonexistent events (which fail silently)."""
    import inspect
    import os
    import re

    import oban as oban_pkg
    from honeybadger.contrib.oban import _LOOP_EXCEPTION_EVENTS

    src_dir = os.path.dirname(inspect.getfile(oban_pkg))
    span_prefixes = set()
    for fname in os.listdir(src_dir):
        if fname.endswith(".py"):
            with open(os.path.join(src_dir, fname)) as f:
                span_prefixes.update(
                    re.findall(r'telemetry\.span\(\s*"([^"]+)"', f.read())
                )

    assert span_prefixes, "found no telemetry.span calls in oban source"
    for evt in _LOOP_EXCEPTION_EVENTS:
        prefix = evt.rsplit(".", 1)[0]  # strip trailing ".exception"
        assert prefix in span_prefixes, (
            f"{evt} does not match any telemetry.span prefix in oban "
            f"{getattr(oban_pkg, '__version__', '?')}: {sorted(span_prefixes)}"
        )


def test_second_instance_init_raises(reset_oban_registry):
    from honeybadger.contrib.oban import ObanHoneybadger

    hb1 = ObanHoneybadger()
    hb1.init()
    try:
        hb2 = ObanHoneybadger()
        with pytest.raises(RuntimeError, match="already initialized"):
            hb2.init()
    finally:
        hb1.tearDown()


def test_init_succeeds_after_previous_teardown(reset_oban_registry):
    from honeybadger.contrib.oban import ObanHoneybadger

    hb1 = ObanHoneybadger()
    hb1.init()
    hb1.tearDown()

    hb2 = ObanHoneybadger()
    hb2.init()  # must not raise
    hb2.tearDown()


def test_same_instance_init_is_idempotent(reset_oban_registry, with_insights):
    from oban import telemetry
    from honeybadger.contrib.oban import ObanHoneybadger

    hb = ObanHoneybadger()
    hb.init()
    hb.init()  # second call should early-return
    try:
        handlers = telemetry.core._handlers.get("oban.job.stop", [])
        ids = [h_id for (h_id, _func) in handlers]
        # Only one registration of our handler ID.
        assert ids.count("honeybadger-oban-jobs") == 1
    finally:
        hb.tearDown()


def test_teardown_on_uninitialized_is_noop(reset_oban_registry):
    from honeybadger.contrib.oban import ObanHoneybadger

    hb = ObanHoneybadger()
    hb.tearDown()  # must not raise


def test_teardown_fully_reverses_init(reset_oban_registry, with_insights):
    from oban import Oban, worker, telemetry
    from oban._extensions import _extensions
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class TDW:
        async def process(self, job):
            return None

    original_enqueue_many = Oban.enqueue_many
    original_process = TDW.process

    hb = ObanHoneybadger(report_exceptions=True)
    hb.init()

    # Sanity: wiring is in place.
    assert Oban.enqueue_many is not original_enqueue_many
    assert TDW.process is not original_process
    assert "executor.wrap_result" in _extensions
    assert telemetry.core._handlers.get("oban.job.stop")

    hb.tearDown()

    # All four wiring sites reversed.
    assert Oban.enqueue_many is original_enqueue_many
    assert TDW.process is original_process
    # executor.wrap_result: restored to identity (or whatever was prior).
    # We installed nothing prior, so it should equal the identity stored at init time.
    assert hb._wrap_result_installed is False
    # Verify the executor.wrap_result extension was actually restored to
    # identity behavior, not just the flag cleared.
    from oban._extensions import get_ext

    restored = get_ext("executor.wrap_result", None)
    sentinel = object()
    assert restored(None, sentinel) is sentinel
    # Telemetry handlers detached.
    handler_ids = [
        h_id for (h_id, _func) in telemetry.core._handlers.get("oban.job.stop", [])
    ]
    assert "honeybadger-oban-jobs" not in handler_ids


def test_plugin_generate_payload_filters_sensitive_keys():
    from honeybadger.contrib.oban import ObanPlugin, _current_job_var
    from unittest.mock import MagicMock

    job = MagicMock()
    job.id = 1
    job.worker = "MyW"
    job.queue = "default"
    job.attempt = 1
    job.max_attempts = 5
    job.args = {"user_id": 7, "password": "secret", "nested": {"password": "n"}}
    job.meta = {"token": "xyz", "password": "p"}
    job.tags = []

    token = _current_job_var.set(job)
    try:
        plugin = ObanPlugin()
        payload = plugin.generate_payload({"request": {}}, {}, {})
    finally:
        _current_job_var.reset(token)

    request = payload["request"]
    # Top-level matching keys are removed (remove_keys=True only applies at top level).
    assert "password" not in request["params"]["args"]
    assert "password" not in request["params"]["meta"]
    assert request["params"]["args"]["user_id"] == 7
    # Nested matching keys are replaced with "[FILTERED]" (filter_dict's recursion
    # does not propagate remove_keys).
    assert request["params"]["args"]["nested"]["password"] == "[FILTERED]"
    # job state must NOT be mutated by the plugin's filtering.
    assert job.args["password"] == "secret"
    assert job.args["nested"]["password"] == "n"
    assert job.meta["password"] == "p"


@pytest.mark.asyncio
async def test_include_args_deep_copy_does_not_mutate_nested(
    reset_oban_registry, with_insights
):
    from oban import worker, telemetry
    from oban.job import Job
    from honeybadger import honeybadger as hb_module
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class ArgsNestW:
        async def process(self, job):
            return None

    hb_module.config.insights_config.oban.include_args = True

    hb = ObanHoneybadger()
    hb.init()
    try:
        job = Job(
            "ArgsNestW",
            args={
                "user_id": 7,
                "password": "secret",
                "nested": {"inner_pw": None, "password": "deep"},
            },
            id=20,
            queue="default",
            attempt=1,
            max_attempts=5,
            meta={"token": "xyz", "ok": True},
            tags=[],
        )
        with patch("honeybadger.contrib.oban.honeybadger.event"):
            telemetry.execute(
                "oban.job.stop",
                {
                    "job": job,
                    "state": "completed",
                    "duration": 0,
                    "queue_time": 0,
                    "monotonic_time": 0,
                },
            )
        # Nested dict in job.args must NOT be mutated.
        assert job.args["nested"]["password"] == "deep"
        assert job.args["password"] == "secret"
    finally:
        hb.tearDown()


@pytest.mark.asyncio
async def test_teardown_leaves_later_wrap_result_extension_in_place(
    reset_oban_registry,
):
    from oban._extensions import get_ext, put_ext
    from honeybadger.contrib.oban import ObanHoneybadger

    hb = ObanHoneybadger(report_exceptions=True)
    hb.init()

    def later_ext(job, result):
        return result

    put_ext("executor.wrap_result", later_ext)
    hb.tearDown()

    # Later integration's extension should still be installed.
    current = get_ext("executor.wrap_result", None)
    assert current is later_ext


def test_teardown_leaves_later_after_register_extension_in_place(reset_oban_registry):
    from oban._extensions import get_ext, put_ext
    from honeybadger.contrib.oban import ObanHoneybadger

    hb = ObanHoneybadger()
    hb.init()

    def later_ext(cls):
        pass

    put_ext("worker.after_register", later_ext)
    hb.tearDown()

    current = get_ext("worker.after_register", None)
    assert current is later_ext


@pytest.mark.asyncio
async def test_wrap_result_inert_after_teardown_via_chained_extension(
    reset_oban_registry,
):
    """After teardown, even if a later integration chains us, our extension must not notify."""
    from oban import worker
    from oban.job import Job
    from oban._executor import Executor
    from oban._extensions import get_ext, put_ext
    from honeybadger.contrib.oban import ObanHoneybadger

    @worker(queue="default")
    class InertW:
        async def process(self, job):
            raise ValueError("after teardown")

    hb = ObanHoneybadger(report_exceptions=True)
    hb.init()

    # A later integration captures our handler via the chain pattern.
    captured_prev = get_ext("executor.wrap_result", lambda _j, r: r)

    def chained(job, result):
        return captured_prev(job, result)

    put_ext("executor.wrap_result", chained)

    # Now tear down. The chained later-extension still holds a reference to our handler.
    hb.tearDown()

    # Run a failing job. Our extension's closure will still be called via the chain,
    # but it must NOT call honeybadger.notify.
    from oban.worker import worker_name

    job = Job(worker_name(InertW), args={}, id=1, attempt=1)
    with patch("honeybadger.contrib.oban.honeybadger.notify") as notify:
        await Executor(job).execute()
    assert notify.call_count == 0


def test_after_register_inert_after_teardown_via_chained_extension(reset_oban_registry):
    """After teardown, late-registered workers must not get wrapped by our chain."""
    from oban import worker
    from oban._extensions import get_ext, put_ext
    from honeybadger.contrib.oban import ObanHoneybadger

    hb = ObanHoneybadger()
    hb.init()

    captured_prev = get_ext("worker.after_register", lambda _cls: None)

    def chained(cls):
        captured_prev(cls)

    put_ext("worker.after_register", chained)

    hb.tearDown()

    @worker(queue="default")
    class LateW:
        async def process(self, job):
            return None

    # If our chain had wrapped LateW, _honeybadger_process_wrapped would be set.
    assert not getattr(LateW, "_honeybadger_process_wrapped", False)


def test_is_worker_excluded_handles_none_patterns():
    """exclude_workers=None (dataclass field is not type-validated) must not crash."""
    from honeybadger.contrib.oban import ObanHoneybadger

    assert ObanHoneybadger._is_worker_excluded("any.Worker", None) is False
    assert ObanHoneybadger._is_worker_excluded("any.Worker", []) is False


def test_init_failure_releases_active_instance_and_unwinds_partial(reset_oban_registry):
    """A failure mid-init() must release the single-instance lock and unwind partials."""
    from oban import Oban, worker
    from honeybadger import honeybadger as hb_module
    from honeybadger.config import Configuration
    from honeybadger.contrib.oban import ObanHoneybadger
    import honeybadger.contrib.oban as hb_oban
    import oban.telemetry as oban_telemetry

    @worker(queue="default")
    class PartialW:
        async def process(self, job):
            return None

    original_process = PartialW.process
    original_enqueue_many = Oban.enqueue_many
    original_attach = oban_telemetry.attach

    hb_module.configure(insights_enabled=True)

    def boom(*args, **kwargs):
        raise RuntimeError("boom from attach")

    oban_telemetry.attach = boom
    hb = ObanHoneybadger(report_exceptions=True)
    try:
        with pytest.raises(RuntimeError, match="boom from attach"):
            hb.init()

        # Single-instance lock released, init flag still False.
        assert hb_oban._active_instance is None
        assert hb._initialized is False

        # Partial wiring reverted: worker process restored, enqueue_many restored.
        assert PartialW.process is original_process
        assert Oban.enqueue_many is original_enqueue_many

        # A fresh init on a new instance must succeed after the prior failure.
        oban_telemetry.attach = original_attach
        hb2 = ObanHoneybadger()
        hb2.init()
        hb2.tearDown()
    finally:
        oban_telemetry.attach = original_attach
        hb_module.config = Configuration()
