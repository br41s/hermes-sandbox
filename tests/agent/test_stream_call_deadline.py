"""A streaming API call must have an overall deadline, not just per-read and stale timeouts.

THE BUG THIS CLOSES

2026-09-30: three ``auditor-review`` cron runs (job ``c19bb95c0a62``) each sat in one
OpenRouter stream until the 1800s cron ceiling (``HERMES_CRON_MAX_RUNTIME``). The
ceiling's stack dump had the agent thread in ``ssl.recv`` under
``_call_chat_completions``'s ``for chunk in`` loop. The ceiling itself logged
``idle 1s``, ``last_activity=receiving stream response``: a real chunk landed one second
before the kill. No ``Stream stale`` line appeared in 30 minutes.

Neither streaming guard can see a provider that keeps trickling real chunks:

- the httpx read timeout (``request_timeout_seconds``, 600s) is PER OPERATION, and
  every read returns well inside it;
- the stale detector measures the gap BETWEEN chunks, and each chunk resets it.

#317 gave the non-streaming inline path an overall deadline from the same
``request_timeout_seconds``. Cron turns stream (#90202), so they never got it.

The server below reproduces the drip on a real socket through the real OpenAI client
and httpx: one valid chunk every 0.1s, far inside both the read timeout and the stale
budget, for up to 30 seconds.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import run_agent
from agent import chat_completion_helpers as helpers
from agent.error_classifier import FailoverReason, classify_api_error


def _chunk(delta: dict, finish_reason=None) -> bytes:
    body = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return f"data: {json.dumps(body)}\n\n".encode()


class _DripWire:
    """OpenAI-wire SSE server that sends ``chunks`` content chunks 0.1s apart, then finishes."""

    def __init__(self, chunks: int):
        self.completions = 0
        wire = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", 0)))
                if not self.path.endswith("/chat/completions"):
                    self.send_response(404)  # local capability probes
                    self.end_headers()
                    return
                wire.completions += 1
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(_chunk({"role": "assistant", "content": ""}))
                    for _ in range(chunks):
                        time.sleep(0.1)
                        self.wfile.write(_chunk({"content": "x"}))
                        self.wfile.flush()
                    self.wfile.write(_chunk({}, finish_reason="stop"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass  # the client aborted the stream

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _agent(base_url: str, platform: str):
    return run_agent.AIAgent(
        api_key="test-key", base_url=base_url, model="m", provider="custom", platform=platform,
        quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=[], max_iterations=1,
    )


_KW = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def deadline(monkeypatch):
    """Set the configured ``request_timeout_seconds``; the mechanism is the same at 600s."""
    def _set(seconds):
        monkeypatch.setattr(helpers, "get_provider_request_timeout", lambda *_a, **_k: seconds)
    # The stale detector stays armed, and every drip gap (0.1s) is far inside its budget,
    # so only the deadline can stop the drip. The budget also bounds time-to-first-byte,
    # which reached 1.26s on a loaded CI runner (8 slices in parallel): at 1.0s the
    # detector re-opened the stream before the deadline could fire.
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "2.5")
    return _set


# "cron" streams inline with a monitor thread; "cli" runs the request on a worker thread.
@pytest.mark.parametrize("platform", ["cron", "cli"])
def test_a_dripping_stream_is_aborted_at_the_call_deadline(deadline, platform):
    deadline(1.5)
    wire = _DripWire(chunks=300)  # 30s of drip
    try:
        agent = _agent(wire.base_url, platform)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="deadline exceeded") as excinfo:
            helpers.interruptible_streaming_api_call(agent, dict(_KW))
        elapsed = time.monotonic() - started
    finally:
        wire.close()

    assert elapsed < 6.0, f"took {elapsed:.1f}s: the deadline did not bound the call"
    # The turn loop owns recovery: re-opening the stream would hand the drip the whole
    # budget again (HERMES_STREAM_RETRIES defaults to 2 more attempts).
    assert wire.completions == 1, f"the stream was re-opened {wire.completions - 1} time(s)"
    # It must reach the loop as a retryable timeout, never as a disconnect that a large
    # session reads as context overflow (-> compression -> a rebuilt, uncached prefix).
    verdict = classify_api_error(excinfo.value, provider="openrouter", model="deepseek/deepseek-v4.1-flash",
                                 approx_tokens=190_000, context_length=200_000, num_messages=400)
    assert verdict.reason == FailoverReason.timeout
    assert verdict.retryable and not verdict.should_compress


@pytest.mark.parametrize("configured", [6.0, None], ids=["inside-deadline", "no-deadline-configured"])
def test_a_slow_stream_that_finishes_is_untouched(deadline, configured):
    deadline(configured)
    wire = _DripWire(chunks=30)  # 3s: slower than the stale budget in total, never between chunks
    try:
        agent = _agent(wire.base_url, "cron")
        response = helpers.interruptible_streaming_api_call(agent, dict(_KW))
        # A reused agent's next call gets a fresh clock, not the previous call's deadline.
        second = helpers.interruptible_streaming_api_call(agent, dict(_KW))
    finally:
        wire.close()

    assert response.choices[0].message.content == "x" * 30
    assert response.choices[0].finish_reason == "stop"
    assert second.choices[0].message.content == "x" * 30
    assert wire.completions == 2
