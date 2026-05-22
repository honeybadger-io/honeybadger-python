# Oban contrib demo

A self-contained sample that exercises `honeybadger.contrib.oban` end-to-end:
error reporting via the `executor.wrap_result` extension, per-job Insights
events, maintenance-loop events, and event-context propagation from enqueuer
to worker.

Requires Python ≥ 3.12 (the `oban` package's own constraint) and a running
PostgreSQL. A `docker-compose.yml` is included for a one-command Postgres on
port 5439 so it won't clash with any host instance.

## Setup

```sh
cd examples/oban_app

# 1. Bring up Postgres (non-default port 5439).
docker compose up -d

# 2. Create a Python 3.12 venv and install deps. From inside this directory:
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ../..        # the in-repo honeybadger build
pip install -r requirements.txt

# 3. Install the Oban schema (one-time per database).
python -m oban install --dsn "postgresql://oban:oban@localhost:5439/oban_demo"
```

## Run

```sh
HONEYBADGER_API_KEY=hbp_your_real_key_here python app.py
```

This will:

1. Configure Honeybadger (API key from `HONEYBADGER_API_KEY`) with
   `insights_enabled=True`.
2. Init `ObanHoneybadger(report_exceptions=True)`.
3. Start Oban with a `default` and `reports` queue.
4. Enqueue a mix of:
   - A successful `SendEmailWorker` job (no event context).
   - A failing `FlakyWorker` job that always raises (exercises error
     reporting through `executor.wrap_result`).
   - A `@job`-decorated `generate_report` function.
5. Re-enqueue the same set with a Honeybadger event context set, so the
   propagation path lights up.
6. Sleep ~15s while Oban processes everything, then shut down cleanly.

Watch the console: you'll see worker output, Honeybadger's debug logs about
event batches being sent, and any 4xx/5xx responses from the API. In your
Honeybadger project you should see:

- An `oban.job_finished` event per job with `worker`, `queue`, `state`,
  `duration`, `queue_time`, `attempt`, `max_attempts`, `tags`, and (for
  failures) `error_type` / `error_message`.
- Events emitted during the second batch carry `request_id` and `user_id`
  from the propagated event context.
- Error notices for each `FlakyWorker` run, enriched by `ObanPlugin` with
  component, action, params, and job context.

## Tear down

```sh
docker compose down -v   # also drop the volume
```
