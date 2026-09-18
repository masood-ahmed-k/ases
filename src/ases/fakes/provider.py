"""A scripted fake OpenAI-compatible server for tests (section 14.4/22.0; ASES-TST-01).

The whole point: the controller's test suite never touches a real provider or spends real quota. This
server runs on a background thread on an ephemeral localhost port, serves /v1/chat/completions and
/v1/models, and pops canned responses off a queue in order -- rate limits, 5xx, auth failures, and
malformed JSON are all just entries on that queue. When the queue is empty it serves a default success
response, so a test only has to script the failures it actually cares about.

Stdlib only (http.server), on purpose: this is test infrastructure, not product code, and it should
never need a dependency the rest of ASES doesn't already have.
"""
from __future__ import annotations

import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclasses.dataclass
class ScriptedResponse:
    status: int = 200
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    body: bytes | None = None          # raw bytes wins over body_json if both are set
    body_json: dict | None = None

    def render(self) -> bytes:
        if self.body is not None:
            return self.body
        if self.body_json is not None:
            return json.dumps(self.body_json).encode("utf-8")
        return b""


def default_chat_completion(model: str = "fake-model") -> ScriptedResponse:
    return ScriptedResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body_json={
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        },
    )


def rate_limited(retry_after: int = 1) -> ScriptedResponse:
    return ScriptedResponse(
        status=429,
        headers={"Retry-After": str(retry_after), "Content-Type": "application/json"},
        body_json={"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}},
    )


def server_error(status: int = 500) -> ScriptedResponse:
    return ScriptedResponse(
        status=status,
        headers={"Content-Type": "application/json"},
        body_json={"error": {"message": f"upstream error {status}", "type": "server_error"}},
    )


def auth_failure() -> ScriptedResponse:
    return ScriptedResponse(
        status=401,
        headers={"Content-Type": "application/json"},
        body_json={"error": {"message": "invalid api key", "type": "invalid_request_error"}},
    )


def malformed() -> ScriptedResponse:
    return ScriptedResponse(status=200, headers={"Content-Type": "application/json"}, body=b"{not json")


class FakeProvider:
    """Usage:
        provider = FakeProvider()
        provider.enqueue(rate_limited(), rate_limited(), default_chat_completion())
        provider.start()
        ...  # point a client at provider.base_url
        assert provider.request_count == 3
        provider.stop()
    Also usable as a context manager.
    """

    def __init__(self) -> None:
        self._queue: list[ScriptedResponse] = []
        self._lock = threading.Lock()
        self._requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def enqueue(self, *responses: ScriptedResponse) -> None:
        with self._lock:
            self._queue.extend(responses)

    def _pop_response(self) -> ScriptedResponse:
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
        return default_chat_completion()

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self._requests)

    @property
    def requests(self) -> list[dict]:
        with self._lock:
            return list(self._requests)

    def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: A003 - silence stdlib's default access log
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(length) if length else b""
                with provider._lock:
                    provider._requests.append(
                        {"path": self.path, "method": self.command, "body": raw_body}
                    )
                response = provider._pop_response()
                self.send_response(response.status)
                for k, v in response.headers.items():
                    self.send_header(k, v)
                rendered = response.render()
                self.send_header("Content-Length", str(len(rendered)))
                self.end_headers()
                self.wfile.write(rendered)

            def do_POST(self):  # noqa: N802 - stdlib naming convention
                self._handle()

            def do_GET(self):  # noqa: N802
                self._handle()

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("start() the provider before reading base_url")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeProvider":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
