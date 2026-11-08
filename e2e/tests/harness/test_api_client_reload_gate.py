"""Harness self-tests for the admin API client's handling of the reload gate's retriable
``503 reloading`` answer and of a leaving worker's ``503 leaving`` refusal, driven against an
in-process server that answers the reload gate's 503 (its ``Retry-After`` header and
``{"error": ..., "reloading": true}`` body) for a set number of requests and then its real
answer. The ``503 leaving`` refusal carries neither the header nor the ``reloading`` key: the
client raises it as :class:`WorkerLeavingError` at once, never polling past it as it polls past
the reload gate."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tai42_e2e.httpapi import ApiClient, WorkerLeavingError

pytestmark = pytest.mark.needs("no-stack")

_RELOADING_BODY = {"error": "reloading — the server is applying a config reload; retry shortly", "reloading": True}
_LEAVING_BODY = {
    "error": "leaving — this worker is leaving (a fleet recycle op) and begins no change; retry shortly",
    "leaving": "a fleet recycle op",
}
_REFUSED_BODY = {"error": "refused"}


class _ReloadGateServer:
    """Answers every request with the reload gate's ``503 reloading`` while :attr:`gated` is
    above zero (one fewer per request), then with ``answer_status`` and ``answer_body``."""

    def __init__(self, *, gated: int, answer_status: int, answer_body: Mapping[str, object] = _REFUSED_BODY) -> None:
        self.gated = gated
        self.requests = 0
        server = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                server.requests += 1
                if server.gated > 0:
                    server.gated -= 1
                    self._answer(503, _RELOADING_BODY, {"Retry-After": "5"})
                else:
                    self._answer(answer_status, answer_body, {})

            def _answer(self, status: int, body: Mapping[str, object], headers: dict[str, str]) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> _ReloadGateServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def gated_twice() -> Iterator[_ReloadGateServer]:
    with _ReloadGateServer(gated=2, answer_status=400) as server:
        yield server


async def test_a_raw_request_is_answered_past_the_reload_gate_by_default(gated_twice: _ReloadGateServer) -> None:
    response = await ApiClient(gated_twice.url).request_raw("POST", "/api/things", json={})

    assert response.status_code == 400, response.text
    assert response.json() == _REFUSED_BODY
    assert gated_twice.requests == 3


async def test_a_raw_request_that_opts_out_returns_the_reload_gate_refusal(gated_twice: _ReloadGateServer) -> None:
    response = await ApiClient(gated_twice.url).request_raw("POST", "/api/things", json={}, retry_on_reloading=False)

    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == "5"
    assert response.json() == _RELOADING_BODY
    assert gated_twice.requests == 1


async def test_an_expected_refusal_is_returned_past_the_reload_gate(gated_twice: _ReloadGateServer) -> None:
    body = await ApiClient(gated_twice.url).request("POST", "/api/things", json={}, expect=400)

    assert body == _REFUSED_BODY
    assert gated_twice.requests == 3


async def test_a_leaving_503_raises_the_typed_error_and_is_not_polled_past() -> None:
    with _ReloadGateServer(gated=0, answer_status=503, answer_body=_LEAVING_BODY) as server:
        with pytest.raises(WorkerLeavingError) as raised:
            await ApiClient(server.url).request("POST", "/api/things", json={})

        assert raised.value.reason == "a fleet recycle op"
        assert server.requests == 1
