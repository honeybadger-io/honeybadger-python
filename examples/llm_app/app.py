"""End-to-end demo: emits llm.chat events to Honeybadger Insights.

Usage:
    HONEYBADGER_API_KEY=... python app.py
No OpenAI key needed — a local stub serves canned completions.
"""
import json
import os
import threading

from flask import Flask, request, jsonify

stub = Flask("openai-stub")


@stub.post("/chat/completions")
def chat():
    if request.json.get("stream"):
        def sse():
            for text in ("Hello", " from", " the stub"):
                yield "data: %s\n\n" % json.dumps({
                    "id": "c1", "object": "chat.completion.chunk", "created": 1,
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": {"content": text},
                                 "finish_reason": None}],
                })
            yield "data: %s\n\n" % json.dumps({
                "id": "c1", "object": "chat.completion.chunk", "created": 1,
                "model": "gpt-4o", "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3,
                          "total_tokens": 11},
            })
            yield "data: [DONE]\n\n"
        return stub.response_class(sse(), mimetype="text/event-stream")
    return jsonify({
        "id": "chatcmpl-demo", "object": "chat.completion", "created": 1,
        "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"role": "assistant",
                     "content": "Hello from the stub"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    })


def main():
    threading.Thread(
        target=lambda: stub.run(port=8899), daemon=True
    ).start()

    from honeybadger import honeybadger
    honeybadger.configure(
        api_key=os.environ["HONEYBADGER_API_KEY"],
        insights_enabled=True,
        force_report_data=True,
        insights_config={"llm": {"include_prompts": True,
                                 "include_responses": True}},
    )
    from honeybadger.contrib.llm import LLMHoneybadger
    llm = LLMHoneybadger(instruments=["openai"]).init()

    import openai
    client = openai.OpenAI(api_key="sk-stub", base_url="http://127.0.0.1:8899")

    client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "Say hello"}]
    )
    stream = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "Stream hello"}],
        stream=True, stream_options={"include_usage": True},
    )
    for _ in stream:
        pass

    # tearDown() flushes the otel span pipeline, which synchronously turns
    # each finished span into a honeybadger.event() call (see
    # honeybadger/contrib/llm/_bridge.py:_export_one). That only *enqueues*
    # the events on Honeybadger's own EventsWorker background thread, so we
    # also shut that worker down here to force it to deliver (or attempt to
    # deliver) its queued batch before the process exits, rather than relying
    # on the atexit hook to do it after `main()` has already returned.
    llm.tearDown()
    honeybadger.shutdown()
    print("Done — check Insights for llm.chat events.")


if __name__ == "__main__":
    main()
