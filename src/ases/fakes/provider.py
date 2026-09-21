"""A scripted fake OpenAI-compatible server for tests (section 14.4/22.0; ASES-TST-01).

The whole point: the controller's test suite never touches a real provider or spends real quota. This
server runs on a background thread on an ephemeral localhost port, serves /v1/chat/completions and
/v1/models, and pops canned responses off a queue in order -- rate limits, 5xx, auth failures, and
malformed JSON are all just entries on that queue. When the queue is empty it serves a default success
response, so a test only has to script the failures it actually cares about.

Blueprint 14.4 lists what the server must be able to return: "scripted successes, 429 with Retry-After, 401, 500,
timeouts and malformed JSON". Beyond the plain status responses, the queue can hold a tool call (what a worker's model
answers when it wants to run a tool), a slow response (`delay_seconds`, for a client timeout) and a dropped connection
(no response at all). Every request the server receives is kept in `requests` (body, path, method and the headers with
credentials redacted), and `assert_never_received` proves that a planted secret never reached any prompt (acceptance
22.10: "Neither may appear in any prompt captured by the fake provider").

Stdlib only (http.server), on purpose: this is test infrastructure, not product code, and it should
never need a dependency the rest of ASES doesn't already have. It answers with plain (non-streaming) JSON only; a
Hermes worker that insists on streaming needs more than this.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import socket
import sys
import threading
import time
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclasses.dataclass
class ScriptedResponse:
    status: int = 200
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    body: bytes | None = None          # raw bytes wins over body_json if both are set
    body_json: dict | None = None
    delay_seconds: float = 0.0         # the server waits this long before answering (a slow provider, for client timeouts)
    drop_connection: bool = False      # close the connection without answering at all

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


def default_models(models: Iterable[str] = ("fake-model",)) -> ScriptedResponse:
    """What GET /v1/models answers when nothing is scripted for it: the model ids the provider claims to serve."""
    return ScriptedResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body_json={
            "object": "list",
            "data": [{"id": name, "object": "model", "owned_by": "fake"} for name in models],
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


# Conveniences named the way the blueprint names the cases (429 with Retry-After, 401, 500, malformed JSON).
def rate_limit(retry_after: int = 1) -> ScriptedResponse:
    """429 with a Retry-After header of `retry_after` seconds."""
    return rate_limited(retry_after)


def unauthorized() -> ScriptedResponse:
    """401, an invalid API key."""
    return auth_failure()


def malformed_json() -> ScriptedResponse:
    """200 with a body that is not valid JSON."""
    return malformed()


def tool_calls_response(
    calls: Iterable[tuple[str, dict | str]], *, model: str = "fake-model", content: str | None = None,
) -> ScriptedResponse:
    """A chat completion in which the model asks to run tools: `calls` is (function name, arguments) pairs, where the
    arguments are a dict (sent as the JSON string OpenAI's API uses) or already a string. finish_reason is tool_calls."""
    tool_calls = []
    for number, (name, arguments) in enumerate(calls, start=1):
        tool_calls.append({
            "id": f"call_fake_{number}",
            "type": "function",
            "function": {"name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)},
        })
    return ScriptedResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body_json={
            "id": "chatcmpl-fake-tools",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        },
    )


def tool_call_response(
    name: str, arguments: dict | str, *, model: str = "fake-model", content: str | None = None,
) -> ScriptedResponse:
    """A chat completion whose model calls one tool: tool_call_response("terminal", {"command": "curl example.invalid"})."""
    return tool_calls_response([(name, arguments)], model=model, content=content)


def slow_response(delay_seconds: float, inner: ScriptedResponse | None = None) -> ScriptedResponse:
    """`inner` (default: a plain success) after waiting `delay_seconds`: for a client that must time out."""
    response = copy.deepcopy(inner) if inner is not None else default_chat_completion()
    response.delay_seconds = delay_seconds
    return response


def connection_drop() -> ScriptedResponse:
    """The server accepts the request and closes the connection without a byte in reply: the client sees a dropped
    connection (a reset or "remote end closed connection without response"), the other way a provider can time out."""
    return ScriptedResponse(drop_connection=True)


# Headers whose value is a credential. Any header whose NAME mentions one of these words is treated as one too.
_CREDENTIAL_WORDS = ("authorization", "api-key", "apikey", "token", "secret", "cookie", "credential", "x-goog-api")


def _redact_headers(headers) -> dict[str, str]:
    """The request headers as a dict, with the value of every credential-looking header replaced: the provider key is
    supposed to arrive in Authorization, and the log must never become a second place it lives."""
    redacted = {}
    for name, value in headers.items():
        lowered = name.lower()
        redacted[name] = "[redacted]" if any(word in lowered for word in _CREDENTIAL_WORDS) else value
    return redacted


def _strings_in(value) -> list[str]:
    """Every string in a parsed JSON value, so a secret is found even where the JSON text escapes it."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings_in(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings_in(item)]
    return []


class _QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that stays quiet about a client that hung up (a timed-out or dropped request), which is
    the normal end of a slow-response test, not an error worth a traceback on stderr."""

    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: D102
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


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

    def __init__(self, models: Iterable[str] = ("fake-model",)) -> None:
        self._queue: list[ScriptedResponse] = []
        self._lock = threading.Lock()
        self._requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.models = tuple(models)

    def enqueue(self, *responses: ScriptedResponse) -> None:
        with self._lock:
            self._queue.extend(responses)

    def _pop_response(self, path: str = "", method: str = "POST") -> ScriptedResponse:
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
        if method == "GET" and path.split("?", 1)[0].rstrip("/").endswith("/v1/models"):
            return default_models(self.models)
        return default_chat_completion(self.models[0] if self.models else "fake-model")

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self._requests)

    @property
    def requests(self) -> list[dict]:
        """Every request received, oldest first: {path, method, body (raw bytes), headers (credentials redacted), json
        (the body parsed, or None when it is not JSON)}."""
        with self._lock:
            return [dict(request) for request in self._requests]

    def assert_never_received(self, secrets: str | bytes | Iterable[str | bytes]) -> None:
        """Fail unless none of `secrets` appears in any request the provider received (the body, its parsed JSON strings, or
        the URL). Headers are not searched: the provider key legitimately travels in Authorization, and the log redacts it.
        A failure names which secret (by position and length) and which request, and never prints the secret itself."""
        if isinstance(secrets, (str, bytes)):
            secrets = [secrets]
        needles: list[tuple[int, bytes, str]] = []
        for index, secret in enumerate(secrets):
            raw = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
            if not raw:
                raise ValueError("an empty secret would match every request")
            needles.append((index, raw, raw.decode("utf-8", errors="replace")))
        for number, request in enumerate(self.requests):
            haystacks = [request["path"].encode("utf-8"), request["body"]]
            texts = _strings_in(request.get("json"))
            for index, raw, text in needles:
                if any(raw in haystack for haystack in haystacks) or any(text in candidate for candidate in texts):
                    raise AssertionError(
                        f"planted secret #{index} (length {len(raw)}) reached the fake provider in request "
                        f"#{number} ({request['method']} {request['path']})")

    def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: A003 - silence stdlib's default access log
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(length) if length else b""
                try:
                    parsed = json.loads(raw_body) if raw_body else None
                except ValueError:
                    parsed = None
                with provider._lock:
                    provider._requests.append({
                        "path": self.path, "method": self.command, "body": raw_body,
                        "headers": _redact_headers(self.headers), "json": parsed,
                    })
                response = provider._pop_response(self.path, self.command)
                if response.delay_seconds > 0:
                    time.sleep(response.delay_seconds)
                if response.drop_connection:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
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

        self._server = _QuietServer((host, port), Handler)
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

    def hermes_endpoint_config(
        self, model: str | None = None, *, provider_name: str = "ases-fake", context_length: int = 65536,
        api_key: str = "ases-fake-key-not-a-secret",
    ) -> dict:
        """A Hermes config.yaml fragment that points a profile at this server as a custom endpoint, for a later run of a real
        Hermes on an `ases-test` board (this round never runs Hermes). Shape read from hermes_cli/config_providers.py: a
        `providers:` entry with `api` (the base URL including /v1), a dummy key, a default model and a declared context
        length (Hermes rejects a custom endpoint under 64K), and the `model:` section selecting it. The key is a placeholder
        that the fake ignores; it is not a secret."""
        chosen = model or (self.models[0] if self.models else "fake-model")
        return {
            "model": {"default": chosen, "provider": provider_name},
            "providers": {
                provider_name: {
                    "api": f"{self.base_url}/v1",
                    "api_key": api_key,
                    "default_model": chosen,
                    "context_length": context_length,
                },
            },
        }

    def __enter__(self) -> "FakeProvider":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
