"""Exercises the fake provider itself: the test infrastructure that every later phase's controller
tests will build on (ASES-TST-01). No real network call, no real provider, ever."""
import json
import urllib.error
import urllib.request

import pytest

from ases.fakes.provider import (
    FakeProvider,
    auth_failure,
    default_chat_completion,
    malformed,
    rate_limited,
    server_error,
)


def _post(base_url: str, body: bytes = b"{}") -> tuple[int, dict, bytes]:
    req = urllib.request.Request(f"{base_url}/v1/chat/completions", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_default_response_is_a_successful_completion():
    with FakeProvider() as provider:
        status, _headers, body = _post(provider.base_url)
        assert status == 200
        parsed = json.loads(body)
        assert parsed["choices"][0]["message"]["content"] == "ok"
        assert provider.request_count == 1


def test_scripted_queue_is_served_in_order_then_falls_back_to_default():
    with FakeProvider() as provider:
        provider.enqueue(rate_limited(retry_after=2), server_error(500), auth_failure())

        status1, headers1, _ = _post(provider.base_url)
        assert status1 == 429
        assert headers1["Retry-After"] == "2"

        status2, _, _ = _post(provider.base_url)
        assert status2 == 500

        status3, _, _ = _post(provider.base_url)
        assert status3 == 401

        status4, _, body4 = _post(provider.base_url)  # queue now empty -> default success
        assert status4 == 200
        assert json.loads(body4)["choices"][0]["finish_reason"] == "stop"


def test_malformed_response_is_not_valid_json():
    with FakeProvider() as provider:
        provider.enqueue(malformed())
        status, _, body = _post(provider.base_url)
        assert status == 200
        with pytest.raises(json.JSONDecodeError):
            json.loads(body)


def test_requests_are_recorded_with_body():
    with FakeProvider() as provider:
        provider.enqueue(default_chat_completion(model="test-model"))
        _post(provider.base_url, body=b'{"model": "test-model", "messages": []}')
        assert provider.request_count == 1
        assert provider.requests[0]["path"] == "/v1/chat/completions"
        assert b"test-model" in provider.requests[0]["body"]


def test_stop_is_idempotent():
    provider = FakeProvider()
    provider.start()
    provider.stop()
    provider.stop()  # must not raise
