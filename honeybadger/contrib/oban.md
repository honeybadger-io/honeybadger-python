# Oban contrib — maintainer notes

This document describes what `honeybadger/contrib/oban.py` does and the *why* behind its non-obvious choices. It's for someone maintaining or extending the contrib. End-user usage and configuration live in the project [README](../../README.md) under "Oban".

## What the contrib does

Hooks Honeybadger into [Oban](https://github.com/oban-bg/oban-py) so that:

- Unhandled exceptions raised inside a worker's `process()` are reported to Honeybadger as errors, with the worker name, queue, args, attempt, and other job fields attached to the notice (opt-in via `report_exceptions=True`).
- Per-job lifecycle events (`oban.job.stop`, `oban.job.exception`) and selected maintenance-loop failure events are emitted as Honeybadger Insights events when `insights_enabled=True`.
- Any Honeybadger event context set before `Worker.enqueue(...)` / `Oban.enqueue(...)` is propagated through the job's `meta` and re-applied while the job runs and while its Insights events are emitted — so the Insights timeline can link a request to its eventual background work.

## Integration surfaces

Three hook points in Oban do all the work:

1. **`executor.wrap_result`** — Oban's `Executor` catches every exception raised by a worker, stores it as `self.result`, then calls `use_ext("executor.wrap_result", default, job, self.result)` inside `_record_stopped`. The hook receives the live `Exception` instance — this is the only place where we can reach the real exception with its frames. Used for error reporting.

2. **`oban.telemetry`** — Oban emits `oban.job.{start,stop,exception}` and several `oban.<loop>.{stage,prune,…}.{stop,exception}` events. `telemetry.attach(handler_id, [event_names], handler)` subscribes; `telemetry.detach(handler_id)` removes. Used for Insights.

3. **Module-level monkey-patches** — `Oban.enqueue_many` (for write-side context injection) and each registered worker class's `process` (for read-side context application). Patched at `init()`, restored at `tearDown()`.

## Lifecycle

`ObanHoneybadger(report_exceptions=False).init()` wires everything; `tearDown()` reverses everything. Both are idempotent.

`init()` runs under `try/except`: if any step raises (e.g. `ImportError` on Python <3.12 where oban isn't installed, or a sabotaged telemetry attach), `_cleanup_wiring()` best-effort reverts every per-target flag and the single-instance lock is released. The caller can then fix the underlying issue and retry.

Only one `ObanHoneybadger` instance may be active per process at a time. A second `init()` call from a different instance raises `RuntimeError` until the prior instance is torn down.

## Why the non-obvious choices

### Own `ContextVar` for the current job instead of `Executor.current_job()`

Oban's `_executor` exposes a `ContextVar` that's set inside `Executor._process` and reset in its `finally`. That reset runs *before* `_record_stopped` invokes `executor.wrap_result`, so `Executor.current_job()` returns `None` by the time our extension would read it. We maintain `_current_job_var` in this module and set it ourselves inside both the per-worker wrap (for user code that calls `honeybadger.notify` from within `process`) and the `wrap_result` extension (so `ObanPlugin` can enrich the auto-`notify` payload).

### Three sites that apply propagated event context

The per-worker wrap, the `wrap_result` extension, and the `_on_job_event` telemetry handler each apply `job.meta["honeybadger_event_context"]` via the shared `_event_context_from_meta` contextmanager. There's no single Oban hook that wraps all three call windows: the wrap fires before `process()`; `wrap_result` fires after `process()` returns; `_on_job_event` fires later still, from `_report_stopped`. Each site is self-contained. `honeybadger.event_context` is token-based, so a pre-existing context set by an outer caller is restored on exit instead of cleared.

### Patch `Oban.enqueue_many` (only)

Every enqueue path funnels through `Oban.enqueue_many` — `Oban.enqueue(job)` delegates to it; `Worker.enqueue` and `@job`-wrapped `enqueue` both call `Oban.get_instance(...).enqueue(...)`. Patching this single method covers transactional inserts (`conn=...`), bulk inserts, the variadic form, and both decorator surfaces. The wrapper mutates `job.meta` in place because `Job.__slots__` makes `meta` settable but doesn't enforce immutability.

### Wrap worker `process` via the registry, not subclassing

`oban.worker.Worker` is a `typing.Protocol`, not a base class. The `@worker` decorator registers each decorated class into `oban.worker._registry` and fires the `worker.after_register` extension. At `init()` we walk the registry once for already-decorated classes, then `put_ext("worker.after_register", …)` so future `@worker` decorations also get wrapped. The same path works uniformly for `@job` function workers (which the `@job` decorator builds as a `FunctionWorker` class then runs through `@worker`).

### Extension chaining via `get_ext` / `put_ext`

Oban's `_extensions` table allows only one callback per name. To coexist with other integrations (Oban Web, Oban Pro, user-installed extensions), we read the prior callback with `get_ext` before installing our own, and call it first from inside our wrapper. We deliberately let prior-callback exceptions propagate — that matches what would happen without our wrapper in place.

### Wrappers go inert after `tearDown`

A later integration may install its own `executor.wrap_result` extension by capturing ours via `get_ext` and calling us as its prior — i.e. *chaining into* us. After we tear down, the later integration's closure still references our handler and will keep invoking it. Our wrapper gates the auto-`notify` (and the worker-class wrapper gates worker-wrap-on-register) on `self._initialized`, so post-teardown calls become no-ops without breaking the chain.

### `tearDown` identity check before restoring extensions

If another integration installed its own extension *after* our `init()` (without chaining into us), `tearDown` must not clobber it. Before restoring the prior extension we compare via `is` identity to the wrapper we installed at `init()` time. If a stranger replaced us, we leave them alone.

### Single-instance guard

Telemetry handler IDs (`"honeybadger-oban-jobs"`, `"honeybadger-oban-loops"`) are fixed strings. Two instances would create duplicate attachments and double-fire every Insights event. Module-level `_active_instance` enforces one-active-at-a-time; the guard is the first thing `init()` checks after the same-instance early return.

### `exclude_workers` filters Insights events only

Mirrors `CeleryConfig.exclude_tasks`. Errors are independent of Insights configuration; if a user wants to silence a worker's errors, the right knob is `excluded_exceptions` or `before_notify` on the core config.

### `include_args` deep-copies before `filter_dict`

`honeybadger.utils.filter_dict` mutates nested dicts in place. Without a deep copy, redacting `password` from `job.args["user"]` would also corrupt the live Oban job that other downstream code (e.g. retry serialization) may still inspect. The same deep-copy guard is applied in `ObanPlugin.generate_payload` before assembling notice params.

## Configuration

`honeybadger.config.insights_config.oban` is an `ObanConfig` dataclass:

| Field | Default | Effect |
|---|---|---|
| `disabled` | `False` | When `True`, skip all telemetry attachments at `init()`. |
| `exclude_workers` | `[]` | List of strings or compiled regex; matched against `job.worker` (fully-qualified `module.Class`). Filters Insights events only. |
| `include_args` | `False` | When `True`, include filtered `job.args` and `job.meta` in `oban.job_finished` events. |

`ObanHoneybadger(report_exceptions=False).init()` is the constructor knob. Pass `report_exceptions=True` to install the `executor.wrap_result` extension; otherwise errors aren't auto-reported (Insights still works).

## Limitations and gotchas

- **Python 3.12+** — Oban's own requirement. The contrib module imports cleanly on older Pythons (all `oban.*` imports are lazy), but `init()` will raise `ImportError`. The CI matrix on 3.9-3.11 skips the test file via `pytestmark`; `mypy.ini` suppresses `import-not-found` for `oban.*` so the source typechecks on those rows too.
- **One instance per process** — see decision above. Tests must `tearDown()` between cases; the test-suite uses a `reset_oban_registry` fixture that also clears event context to defend against cross-test pollution.
- **`filter_dict` recursion** — `remove_keys=True` only applies at the top level (the recursive call in `honeybadger/utils.py` drops the flag). Nested matching keys are value-replaced with `"[FILTERED]"` rather than removed. Not a leak — just a behavior worth knowing when reading tests.
- **Late-overridden `process`** — the per-worker wrap is installed on each registered class at the moment of registration (or at `init()` time). If user code later replaces `cls.process` directly, our wrapper is shadowed and that class will stop being wrapped. This is acceptable for the documented usage pattern (`@worker` then `process` defined inside the class body).
