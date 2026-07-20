# LLM instrumentation demo

A self-contained sample that exercises `honeybadger.contrib.llm` end to end:
`LLMHoneybadger` auto-instruments the OpenAI SDK, makes one non-streaming and
one streaming chat completion, and emits an `llm.chat` Insights event for
each. No OpenAI account is needed — the script starts a tiny local Flask
server that stands in for the OpenAI API and returns canned completions.

Requires Python >= 3.10 (the `honeybadger[llm]` extra's floor).

## Setup

```sh
cd examples/llm_app

python3 -m venv .venv
source .venv/bin/activate
pip install -e "../..[llm]"       # the in-repo honeybadger build, with the [llm] extra
pip install -r requirements.txt   # openai, flask (and honeybadger[llm] again, harmlessly)
```

(If you'd rather install from PyPI instead of the local checkout, `pip
install -r requirements.txt` alone is enough — `honeybadger[llm]` is listed
there.)

## Run

```sh
HONEYBADGER_API_KEY=hbp_your_real_key_here python app.py
```

This will:

1. Start the stub OpenAI-compatible server on `127.0.0.1:8899` in a
   background thread.
2. Configure Honeybadger (API key from `HONEYBADGER_API_KEY`) with
   `insights_enabled=True` and prompt/response capture turned on.
3. Init `LLMHoneybadger(instruments=["openai"])`, which instruments the
   `openai` SDK so every chat completion call produces an OpenTelemetry span.
4. Make one regular `client.chat.completions.create(...)` call and one
   `stream=True` call against the stub, fully draining the stream.
5. Tear the instrumentation down and shut the Honeybadger events worker down,
   which flushes both the OpenTelemetry span pipeline and the queued
   Insights events before the process exits.

Expected output: the script exits cleanly and prints `Done`. With a real API
key you should see two `llm.chat` events in your Honeybadger project's
Insights shortly after — one with `stream: false`, one with `stream: true`.

## Query in Insights

Once the events land, try this BadgerQL query in your project's Insights
tab:

```
filter event_type::str == "llm.chat" | fields ts, model, input_tokens, output_tokens, duration
```

## Notes

- The stub server intentionally implements only `POST /chat/completions`
  (streaming and non-streaming) — just enough surface for this demo. It is
  not a real OpenAI API replacement.
- Because `include_prompts` / `include_responses` are turned on in
  `honeybadger.configure(...)`, the emitted events include the prompt and
  completion text. Turn those off (or rely on the library defaults, which
  are off) before pointing this at real conversations you don't want
  captured.
