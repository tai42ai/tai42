"""Harness self-tests for the RabbitMQ management client. Its credentials: the celery leg grants
each new vhost to the user its AMQP URL connects as, and a management URL without credentials or an
AMQP URL without a user is refused. Its
per-call bound: a vhost delete the broker answers slower than a few seconds still completes within
the harness's infra-phase deadline, and a delete the broker never answers within it fails loudly,
naming the vhost the abandoned request leaves half-deleted. Its session-start check: a broker that
refuses the harness user names that user and both fixes (set the harness URLs to the conf's
credentials, or recreate the broker so it reads the conf). The grant and delete tests drive an
in-process management stub that records each create and permissions grant and holds each delete
until the test releases it; the session-start check drives a stub that refuses every GET with 401."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tai42_e2e.rabbitx import RabbitAdmin, broker_user
from tai42_e2e.topology import InfraUnavailableError
from tai42_e2e.variants.backends import CeleryVariant
from tai42_e2e.waiting import wait_for

pytestmark = pytest.mark.needs("no-stack")


class _ManagementStub:
    """A management API that records every ``PUT`` path in :attr:`puts` and answers 201, holds
    every ``DELETE /api/vhosts/<name>`` until :attr:`release` is set, then answers 204;
    ``GET /api/vhosts/<name>`` answers 404 once the vhost is deleted."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.deleted: set[str] = set()
        self.held_since: float | None = None
        self.puts: list[str] = []
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def do_PUT(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                stub.puts.append(self.path)
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

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
        return f"http://tai42:tai42@127.0.0.1:{self._server.server_address[1]}"

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


def test_a_leased_vhost_is_granted_to_the_user_the_amqp_url_connects_as(stub: _ManagementStub) -> None:
    port = stub.url.rsplit(":", 1)[-1]
    settings = SimpleNamespace(
        rabbitmq_url="amqp://app:pw@127.0.0.1:5672",
        rabbitmq_management_url=f"http://harness:secret@127.0.0.1:{port}",
        boot_timeout=5.0,
    )

    lease = CeleryVariant().allocate_broker(SimpleNamespace(settings=settings), "grant")  # type: ignore[arg-type]

    assert stub.puts == ["/api/vhosts/tai42_e2e_grant", "/api/permissions/tai42_e2e_grant/app"]
    assert lease is not None
    assert lease.broker_url == "amqp://app:pw@127.0.0.1:5672/tai42_e2e_grant"


def test_an_amqp_url_without_a_user_is_refused() -> None:
    with pytest.raises(ValueError, match="rabbitmq_url must include a user"):
        broker_user("amqp://127.0.0.1:5672")


def test_a_management_url_without_credentials_is_refused() -> None:
    with pytest.raises(ValueError, match="must include a user and a password"):
        RabbitAdmin("http://127.0.0.1:15672", timeout=5.0)


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


class _RefusingHandler(BaseHTTPRequestHandler):
    """A management API that answers every GET with 401, as a broker does to a user it never created."""

    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return None


def test_a_broker_that_refuses_the_harness_user_names_the_user_and_both_fixes() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RefusingHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        settings = SimpleNamespace(
            rabbitmq_management_url=f"http://tai42:tai42@127.0.0.1:{server.server_address[1]}", boot_timeout=5.0
        )
        with pytest.raises(InfraUnavailableError) as raised:
            CeleryVariant().infra_check(settings)  # type: ignore[arg-type]
        message = str(raised.value)
        assert "refused the harness user 'tai42'" in message
        assert "set them to the user and password of deploy/rabbitmq/rabbitmq.conf" in message
        assert "--force-recreate --renew-anon-volumes rabbitmq" in message
    finally:
        server.shutdown()
        server.server_close()
