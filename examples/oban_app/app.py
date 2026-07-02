"""Sample app exercising the honeybadger.contrib.oban integration.

Setup (one-time):

    1. Start Postgres (see docker-compose.yml in this directory):
         docker compose up -d

    2. Install the Oban schema:
         python -m oban install --dsn "postgresql://oban:oban@localhost:5439/oban_demo"

Run:

    python app.py

The script configures Honeybadger with Insights enabled, starts Oban with
two queues, enqueues a mix of successful jobs, failing jobs, and @job
function jobs (some with a Honeybadger event context set), then runs for
~15 seconds while Oban processes them. You should see:

  * `oban.job_finished` Insights events for each job
  * `request_id` propagated into job execution and the resulting events
  * Honeybadger error notices for the failing worker (with worker name,
    args, queue, attempt, etc. enriched by ObanPlugin)
"""

import asyncio
import logging
import os
import uuid

from oban import Oban, job, worker

from honeybadger import honeybadger
from honeybadger.contrib.oban import ObanHoneybadger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("honeybadger").setLevel(logging.DEBUG)
logging.getLogger("oban").setLevel(logging.INFO)

API_KEY = os.environ.get("HONEYBADGER_API_KEY", "hbp_REPLACE_ME_WITH_YOUR_OWN_KEY")

DSN = os.environ.get("OBAN_DSN", "postgresql://oban:oban@localhost:5439/oban_demo")


@worker(queue="default", max_attempts=2)
class SendEmailWorker:
    async def process(self, job):
        # Simulate doing the work.
        await asyncio.sleep(0.1)
        print(f"[SendEmailWorker] sent email to {job.args.get('to')}")
        return None


@worker(queue="default", max_attempts=2)
class FlakyWorker:
    """Always raises so we exercise the error-reporting path."""

    async def process(self, job):
        await asyncio.sleep(0.05)
        raise ValueError(f"boom while processing {job.args!r}")


@job(queue="reports")
def generate_report(report_id: str):
    print(f"[generate_report] generating report {report_id}")


async def main():
    # Configure Honeybadger BEFORE init() so the contrib reads the right flags.
    honeybadger.configure(
        api_key=API_KEY,
        environment="oban-demo",
        insights_enabled=True,
        # Force sending in dev environments (otherwise Honeybadger drops payloads).
        force_report_data=True,
    )

    # Wire up the contrib. report_exceptions=True installs the wrap_result
    # extension that auto-reports unhandled worker exceptions.
    ObanHoneybadger(report_exceptions=True).init()

    pool = await Oban.create_pool(dsn=DSN)
    try:
        async with Oban(
            pool=pool,
            queues={"default": 5, "reports": 2},
        ) as oban:
            # Enqueue a few jobs WITHOUT any honeybadger event context set.
            await SendEmailWorker.enqueue({"to": "anon@example.com", "subject": "Hi"})
            await FlakyWorker.enqueue({"task": "first"})
            await generate_report.enqueue("daily-001")

            # Enqueue more jobs WITH a honeybadger event context, so the
            # Insights timeline links request -> enqueue -> execution.
            request_id = str(uuid.uuid4())
            honeybadger.set_event_context({"request_id": request_id, "user_id": "user-42"})
            try:
                await SendEmailWorker.enqueue({"to": "you@example.com", "subject": "From request"})
                await FlakyWorker.enqueue({"task": "in-request"})
                await generate_report.enqueue("requested-007")
                print(f"[enqueuer] enqueued with request_id={request_id}")
            finally:
                honeybadger.reset_event_context()

            # Give Oban time to process everything and emit telemetry.
            print("[runner] sleeping 15s while Oban processes jobs...")
            await asyncio.sleep(15)
            print("[runner] shutting down")
    finally:
        await pool.close()
        # Drain pending Honeybadger events before exit.
        honeybadger.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
