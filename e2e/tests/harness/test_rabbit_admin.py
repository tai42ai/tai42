"""Harness self-tests for the RabbitMQ management client's per-call bound: a vhost delete the
broker answers slower than a few seconds still completes within the harness's infra-phase
deadline, and a delete the broker never answers within it fails loudly, naming the vhost the
abandoned request leaves half-deleted. Driven against an in-process management stub that holds
each delete until the test releases it."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tai42_e2e.rabbitx import RabbitAdmin
from tai42_e2e.waiting import wait_for

pytestmark = pytest.mark.needs("no-stack")


class _ManagementStub:
    """A management API that holds every ``DELETE /api/vhosts/<name>`` until :attr:`release` is
    set, then answers 204; ``GET /api/vhosts/<name>`` answers 404 once the vhost is deleted."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.deleted: set[str] = set()
        self.held_since: float | None = None
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def do_DELETE(self) -> None:
                stub.held_since = time.monotonic()
                stub.release.wait()
                stub.deleted.add(self.path.rsplit("/", 1)[-1])
                self.send_response(204)
                self.end_headers()

            def do_GET(self) -> None:
                gone = self.path.rsplit("/", 1)[-1] in stub.deleted
                self.send_response(404 if gone else 200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: object) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://guest:guest@127.0.0.1:{self._server.server_address[1]}"

    def held_for(self) -> float:
        return 0.0 if self.held_since is None else time.monotonic() - self.held_since

    def __enter__(self) -> _ManagementStub:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def stub() -> Iterator[_ManagementStub]:
    with _ManagementStub() as management:
        yield management


def test_a_slow_vhost_delete_completes_within_the_phase_deadline(stub: _ManagementStub) -> None:
    errors: list[BaseException] = []

    def _delete() -> None:
        try:
            RabbitAdmin(stub.url, timeout=30.0).delete_vhost("tai42_e2e_slow")
        except BaseException as exc:
            errors.append(exc)

    client = threading.Thread(target=_delete)
    client.start()
    # The broker holds the delete past five seconds — longer than a fixed few-second client bound
    # — while the client, bounded by the phase deadline, keeps waiting for the answer.
    wait_for(lambda: stub.held_for() > 5.5, deadline=15.0, message="the delete never reached the broker")
    assert client.is_alive(), errors
    stub.release.set()
    client.join(timeout=10.0)

    assert not client.is_alive()
    assert errors == []
    assert stub.deleted == {"tai42_e2e_slow"}


def test_a_delete_the_broker_never_answers_in_time_fails_loudly_naming_the_vhost(stub: _ManagementStub) -> None:
    with pytest.raises(RuntimeError, match=r"tai42_e2e_stuck.*half-deleted"):
        RabbitAdmin(stub.url, timeout=0.5).delete_vhost("tai42_e2e_stuck")
