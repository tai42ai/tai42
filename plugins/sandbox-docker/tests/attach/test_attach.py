"""The exec attachment against a fake engine on a real socket, over mTLS, plain TCP and a unix socket.

Each test runs on asyncio and on uvloop (see ``conftest``). The engine peer is a blocking
socket in a thread (see ``engine_peer``), so it keeps writing the command's output after the
attachment ended the command's stdin, as the daemon does.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import socket
import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import trustme
from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError, SandboxStreamChunk

from tai42_sandbox_docker.attach import ExecAttachment
from tai42_sandbox_docker.attach_carrier import AttachCarrier
from tai42_sandbox_docker.attach_tls import TlsCarrier
from tai42_sandbox_docker.engine_endpoint import (
    EngineEndpoint,
    TcpEndpoint,
    UnixSocketEndpoint,
)
from tai42_sandbox_docker.provider import DockerSandbox
from tai42_sandbox_docker.settings import DockerSandboxSettings

from .engine_peer import (
    EngineCerts,
    EnginePeer,
    PeerConnection,
    answer_and_wait,
    count_stdin,
    engine_certs,
    refuse,
    take_one_byte,
)

# Large enough that the client's socket buffer is full when stdin ends.
_PAYLOAD = b"conf-stdin\n" * 400_000

CARRIERS = ["tls", "tcp", "unix"]

# How long a peer waits on the test before it fails instead of hanging.
_PEER_WAIT_SECONDS = 20


@pytest.fixture(scope="module")
def certs() -> EngineCerts:
    return engine_certs()


class _Engine:
    """Starts a fake engine of one carrier kind and opens attachments to it."""

    def __init__(self, kind: str, certs: EngineCerts, tmp_path: Path) -> None:
        self.kind = kind
        self.certs = certs
        self.tmp_path = tmp_path

    def peer(self, script: Callable[[PeerConnection], None]) -> EnginePeer:
        if self.kind == "unix":
            return EnginePeer(script, unix_path=str(self.tmp_path / "engine.sock"))
        return EnginePeer(script, tls=self.certs.server_context if self.kind == "tls" else None)

    def endpoint(self, peer: EnginePeer) -> EngineEndpoint:
        if self.kind == "unix":
            assert peer.unix_path is not None
            return UnixSocketEndpoint(path=peer.unix_path)
        scheme = "https" if self.kind == "tls" else "tcp"
        return TcpEndpoint(url=f"{scheme}://127.0.0.1:{peer.port}", host="127.0.0.1", port=peer.port)

    def listener(self) -> tuple[socket.socket, EngineEndpoint]:
        """A listening socket of this carrier kind that runs no engine, and the endpoint that dials it."""
        if self.kind == "unix":
            path = str(self.tmp_path / "engine.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(1)
            return listener, UnixSocketEndpoint(path=path)
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        scheme = "https" if self.kind == "tls" else "tcp"
        return listener, TcpEndpoint(url=f"{scheme}://127.0.0.1:{port}", host="127.0.0.1", port=port)

    async def open(self, peer: EnginePeer) -> ExecAttachment:
        context = self.certs.client_context if self.kind == "tls" else None
        return await ExecAttachment.open(self.endpoint(peer), "exec-1", ssl_context=context)


@pytest.fixture(params=CARRIERS)
def engine(request: pytest.FixtureRequest, certs: EngineCerts, tmp_path: Path) -> _Engine:
    return _Engine(request.param, certs, tmp_path)


def _small_send_buffer(attachment: ExecAttachment) -> None:
    """Shrink the client's socket send buffer, so most of stdin waits in the loop's transport."""
    sock = attachment._connected().transport.get_extra_info("socket")
    if sock.family != socket.AF_UNIX:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)


def _accept_until_closed(listener: socket.socket) -> None:
    """Take one connection on ``listener`` and read it, answering nothing, until the client ends it."""
    listener.settimeout(_PEER_WAIT_SECONDS)
    conn, _ = listener.accept()
    with conn, listener, contextlib.suppress(ConnectionResetError):  # the client aborts its connection
        conn.settimeout(_PEER_WAIT_SECONDS)
        while conn.recv(1 << 16):
            pass


async def _read_all(attachment: ExecAttachment) -> list[SandboxStreamChunk]:
    chunks = []
    while (chunk := await asyncio.wait_for(attachment.read(), 20)) is not None:
        chunks.append(chunk)
    return chunks


async def test_every_stdin_byte_reaches_the_command_before_the_end_of_its_input(engine: _Engine) -> None:
    peer = engine.peer(count_stdin)
    attachment = await engine.open(peer)
    _small_send_buffer(attachment)

    assert await attachment.write(_PAYLOAD)
    attachment.end_input()
    chunks = await _read_all(attachment)

    assert chunks == [SandboxStreamChunk(stream="stdout", data=str(len(_PAYLOAD)).encode())]
    assert attachment._connected().ended
    await asyncio.to_thread(peer.join)


async def test_the_attach_starts_the_exec_with_the_engine_hijack_request(engine: _Engine) -> None:
    peer = engine.peer(count_stdin)
    attachment = await engine.open(peer)
    attachment.end_input()
    await _read_all(attachment)
    await asyncio.to_thread(peer.join)

    head, _, body = peer.request.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    assert lines[0] == "POST /exec/exec-1/start HTTP/1.1"
    authority = "localhost" if engine.kind == "unix" else f"127.0.0.1:{peer.port}"
    assert {"Host: " + authority, "Connection: Upgrade", "Upgrade: tcp", "Content-Type: application/json"} <= set(
        lines[1:]
    )
    assert json.loads(body) == {"Detach": False, "Tty": False}


async def test_a_command_that_stops_reading_ends_the_stream_and_its_stdin(engine: _Engine) -> None:
    peer = engine.peer(take_one_byte)
    attachment = await engine.open(peer)

    await attachment.write(b"xy")
    chunks = await _read_all(attachment)

    assert chunks == [SandboxStreamChunk(stream="stdout", data=b"x")]
    assert not await attachment.write(b"more")
    with pytest.raises(SandboxError, match="the sandbox exec stream is closed"):
        attachment.end_input()
    await asyncio.to_thread(peer.join)


async def test_a_write_waiting_on_a_full_buffer_stops_once_the_command_has_ended(engine: _Engine) -> None:
    written = threading.Event()

    def script(conn: PeerConnection) -> None:
        # A command that took one byte and exited: the output ends, and stdin is read no more
        # until the client has given up on it.
        conn.answer()
        conn.send_frame(1, conn.sock.recv(1))
        conn.sock.shutdown(socket.SHUT_WR)
        written.wait(_PEER_WAIT_SECONDS)
        conn.wait_for_client_close()

    peer = engine.peer(script)
    attachment = await engine.open(peer)
    _small_send_buffer(attachment)

    taken = await attachment.write(_PAYLOAD * 4)
    written.set()

    assert not taken
    assert await _read_all(attachment) == [SandboxStreamChunk(stream="stdout", data=b"c")]
    await asyncio.to_thread(peer.join)


async def test_ending_stdin_twice_ends_it_once(engine: _Engine) -> None:
    peer = engine.peer(count_stdin)
    attachment = await engine.open(peer)
    assert await attachment.write(b"abc")
    attachment.end_input()
    attachment.end_input()

    assert not await attachment.write(b"late")
    assert await _read_all(attachment) == [SandboxStreamChunk(stream="stdout", data=b"3")]
    await asyncio.to_thread(peer.join)


async def test_output_frames_are_demultiplexed_into_stdout_and_stderr(engine: _Engine) -> None:
    def script(conn: PeerConnection) -> None:
        conn.answer()
        conn.send_frame(1, b"out")
        conn.send_frame(2, b"err")
        conn.send_frame(1, b"")
        conn.end_output()

    peer = engine.peer(script)
    attachment = await engine.open(peer)

    assert await _read_all(attachment) == [
        SandboxStreamChunk(stream="stdout", data=b"out"),
        SandboxStreamChunk(stream="stderr", data=b"err"),
        SandboxStreamChunk(stream="stdout", data=b""),
    ]
    await asyncio.to_thread(peer.join)


async def test_output_the_reader_has_not_taken_holds_the_engine_until_it_is_read(engine: _Engine) -> None:
    frame = b"o" * (1 << 15)

    def script(conn: PeerConnection) -> None:
        conn.answer()
        for _ in range(64):
            conn.send_frame(1, frame)
        conn.end_output()

    peer = engine.peer(script)
    attachment = await engine.open(peer)
    await asyncio.sleep(0.3)  # the engine's output piles up unread

    chunks = await _read_all(attachment)

    assert [chunk.data for chunk in chunks] == [frame] * 64
    await asyncio.to_thread(peer.join)


async def test_taking_the_received_output_returns_it_without_a_read_and_leaves_the_end(engine: _Engine) -> None:
    def script(conn: PeerConnection) -> None:
        conn.answer()
        conn.send_frame(1, b"out")
        conn.send_frame(2, b"err")
        conn.end_output()

    peer = engine.peer(script)
    attachment = await engine.open(peer)
    await asyncio.to_thread(peer.join)
    await asyncio.sleep(0.1)  # the engine's output and its end arrive unread

    taken = attachment.take_received()

    assert taken == [
        SandboxStreamChunk(stream="stdout", data=b"out"),
        SandboxStreamChunk(stream="stderr", data=b"err"),
    ]
    assert attachment.take_received() == []
    assert await asyncio.wait_for(attachment.read(), 20) is None


async def test_taking_the_received_output_frees_an_engine_held_by_unread_output(engine: _Engine) -> None:
    frame = b"o" * (1 << 15)

    def script(conn: PeerConnection) -> None:
        conn.answer()
        for _ in range(64):
            conn.send_frame(1, frame)
        conn.end_output()

    peer = engine.peer(script)
    attachment = await engine.open(peer)
    await asyncio.sleep(0.3)  # the engine's output piles up unread until the attachment stops reading

    taken = attachment.take_received()
    rest = await _read_all(attachment)

    assert taken
    assert [chunk.data for chunk in taken + rest] == [frame] * 64
    await asyncio.to_thread(peer.join)


async def test_a_stream_that_ends_inside_an_output_frame_raises(engine: _Engine) -> None:
    def script(conn: PeerConnection) -> None:
        conn.answer()
        conn.send_frame(1, b"whole")
        conn.sock.sendall(bytes([1, 0, 0, 0, 0, 0, 0, 9]) + b"cut")
        conn.end_output()

    peer = engine.peer(script)
    attachment = await engine.open(peer)

    assert await attachment.read() == SandboxStreamChunk(stream="stdout", data=b"whole")
    with pytest.raises(SandboxError, match=r"ended inside an output frame \(11 bytes left over\)"):
        await _read_all(attachment)
    await asyncio.to_thread(peer.join)


@pytest.mark.parametrize(("status", "message"), [(409, "container is not running"), (404, "No such exec instance")])
async def test_an_engine_refusing_the_attach_raises_its_message(engine: _Engine, status: int, message: str) -> None:
    peer = engine.peer(refuse(status, json.dumps({"message": message}).encode()))

    with pytest.raises(DockerError) as raised:
        await engine.open(peer)

    assert (raised.value.status, raised.value.message) == (status, message)
    await asyncio.to_thread(peer.join)


async def test_a_refusal_whose_body_runs_to_the_connection_end_raises_its_text(engine: _Engine) -> None:
    peer = engine.peer(refuse(500, b"engine trouble", length=False))

    with pytest.raises(DockerError) as raised:
        await engine.open(peer)

    assert (raised.value.status, raised.value.message) == (500, "engine trouble")
    await asyncio.to_thread(peer.join)


async def test_an_engine_that_closes_before_it_answers_raises(engine: _Engine) -> None:
    peer = engine.peer(PeerConnection.end_output)

    with pytest.raises(ConnectionResetError, match="closed the exec attach connection before it answered"):
        await engine.open(peer)
    await asyncio.to_thread(peer.join)


async def test_an_answer_that_is_not_http_raises_without_failing_the_loop_protocol_callback(
    engine: _Engine, caplog: pytest.LogCaptureFixture
) -> None:
    def script(conn: PeerConnection) -> None:
        conn.sock.sendall(b"garbage\r\n\r\n")
        conn.wait_for_client_close()

    peer = engine.peer(script)

    with caplog.at_level(logging.DEBUG, logger="asyncio"):
        with pytest.raises(SandboxError, match="answered the exec attach with 'garbage'"):
            await engine.open(peer)
        await asyncio.to_thread(peer.join)

    # The loop reports a protocol callback that raised as a fatal error of the connection.
    assert [record.getMessage() for record in caplog.records if "Fatal error" in record.getMessage()] == []


async def test_a_cancelled_attach_closes_its_connection(engine: _Engine) -> None:
    peer = engine.peer(PeerConnection.wait_for_client_close)
    opening = asyncio.create_task(engine.open(peer))
    await asyncio.sleep(0.3)  # the request is sent; the engine never answers

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    # The peer's wait ends only once the connection is gone.
    await asyncio.to_thread(peer.join)


async def test_an_attach_cancelled_as_its_connection_is_made_leaves_the_loop_no_error(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener, endpoint = engine.listener()
    context = engine.certs.client_context if engine.kind == "tls" else None
    opening = asyncio.create_task(ExecAttachment.open(endpoint, "exec-1", ssl_context=context))
    made = AttachCarrier.connection_made

    def made_then_cancelled(carrier: AttachCarrier, transport: asyncio.BaseTransport) -> None:
        # The cancel lands after the loop made the connection, before the connect resumes the attach.
        made(carrier, transport)
        opening.cancel()

    monkeypatch.setattr(AttachCarrier, "connection_made", made_then_cancelled)
    loop = asyncio.get_running_loop()
    errors: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _, context: errors.append(context))
    accepting = asyncio.create_task(asyncio.to_thread(_accept_until_closed, listener))

    with pytest.raises(asyncio.CancelledError):
        await opening
    await accepting
    await asyncio.sleep(0.1)  # the connection's loss reaches the carrier
    gc.collect()  # a future no one retrieved reports its exception as it is collected
    await asyncio.sleep(0)

    loop.set_exception_handler(None)
    assert errors == []


async def test_closing_the_attachment_ends_the_stream_and_drops_unsent_stdin(engine: _Engine) -> None:
    peer = engine.peer(answer_and_wait)
    attachment = await engine.open(peer)
    reading = asyncio.create_task(attachment.read())
    await asyncio.sleep(0.1)

    attachment.close()

    assert await reading is None
    assert not await attachment.write(b"after")
    await asyncio.to_thread(peer.join)


async def test_a_cancelled_read_loses_no_output(engine: _Engine) -> None:
    release = asyncio.Event()

    def script(conn: PeerConnection) -> None:
        conn.answer()
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(20)
        conn.send_frame(1, b"late")
        conn.end_output()

    loop = asyncio.get_running_loop()
    peer = engine.peer(script)
    attachment = await engine.open(peer)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(attachment.read(), 0.1)

    release.set()

    assert await _read_all(attachment) == [SandboxStreamChunk(stream="stdout", data=b"late")]
    await asyncio.to_thread(peer.join)


# -- TLS ----------------------------------------------------------------------------------


async def test_an_engine_certificate_the_ca_did_not_sign_fails_the_attach(certs: EngineCerts) -> None:
    impostor = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    trustme.CA().issue_cert("127.0.0.1").configure_cert(impostor)
    peer = EnginePeer(count_stdin, tls=impostor)

    with pytest.raises(ssl.SSLCertVerificationError):
        await ExecAttachment.open(
            TcpEndpoint(url=f"https://127.0.0.1:{peer.port}", host="127.0.0.1", port=peer.port),
            "exec-1",
            ssl_context=certs.client_context,
        )
    with pytest.raises(ssl.SSLError):
        await asyncio.to_thread(peer.join)


def _without_client_identity(certs: EngineCerts) -> ssl.SSLContext:
    """A client context that trusts the engine's CA but presents no client certificate."""
    context = ssl.create_default_context()
    certs.ca.configure_trust(context)
    return context


def _engine_refusing_the_client_identity(certs: EngineCerts) -> tuple[int, Callable[[], None]]:
    """A TLS 1.3 engine that refuses a client with no certificate, and the wait for it to finish.

    Over TLS 1.3 the client's handshake completes before the engine checks its certificate:
    the engine's refusal is an alert the client reads after it. The engine then drains what the
    client sent until the client closes, so the alert is never overtaken by a reset.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(20)
    refusals: list[BaseException] = []

    def serve() -> None:
        conn, _ = listener.accept()
        conn.settimeout(20)
        # The TLS socket takes over ``conn``; its duplicate reads on below the failed TLS.
        raw = conn.dup()
        with certs.server_context.wrap_socket(conn, server_side=True, do_handshake_on_connect=False) as tls:
            try:
                tls.do_handshake()
            except ssl.SSLError as exc:
                refusals.append(exc)
            while raw.recv(4096):
                pass
        raw.close()
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    def finished() -> None:
        thread.join(20)
        assert not thread.is_alive(), "the fake engine did not finish"
        assert [type(exc) for exc in refusals] == [ssl.SSLError], "the engine did not refuse the client"

    return listener.getsockname()[1], finished


async def test_an_engine_refusing_the_client_identity_after_the_handshake_fails_the_attach(
    certs: EngineCerts,
) -> None:
    port, finished = _engine_refusing_the_client_identity(certs)

    with pytest.raises(ssl.SSLError, match="TLSV13_ALERT_CERTIFICATE_REQUIRED"):
        await ExecAttachment.open(
            TcpEndpoint(url=f"https://127.0.0.1:{port}", host="127.0.0.1", port=port),
            "exec-1",
            ssl_context=_without_client_identity(certs),
        )
    await asyncio.to_thread(finished)


async def test_an_engine_that_closes_during_the_tls_handshake_fails_the_attach(certs: EngineCerts) -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def close_at_once() -> None:
        conn, _ = listener.accept()
        conn.close()
        listener.close()

    closing = asyncio.create_task(asyncio.to_thread(close_at_once))
    with pytest.raises(ConnectionError):
        await ExecAttachment.open(
            TcpEndpoint(url=f"https://127.0.0.1:{port}", host="127.0.0.1", port=port),
            "exec-1",
            ssl_context=certs.client_context,
        )
    await closing


async def test_a_corrupt_tls_record_from_the_engine_fails_the_read(certs: EngineCerts) -> None:
    def script(conn: PeerConnection) -> None:
        conn.answer()
        conn.send_frame(1, b"before")
        conn.send_raw(b"\x17\x03\x03\x00\x20" + b"\x00" * 32)  # an application-data record no key sealed
        conn.wait_for_client_close()

    peer = EnginePeer(script, tls=certs.server_context)
    attachment = await ExecAttachment.open(
        TcpEndpoint(url=f"https://127.0.0.1:{peer.port}", host="127.0.0.1", port=peer.port),
        "exec-1",
        ssl_context=certs.client_context,
    )

    assert await attachment.read() == SandboxStreamChunk(stream="stdout", data=b"before")
    with pytest.raises(SandboxError, match="the sandbox exec stream's TLS failed") as raised:
        await _read_all(attachment)
    assert isinstance(raised.value.__cause__, ssl.SSLError)
    await asyncio.to_thread(peer.join)


async def test_a_tls_layer_that_refuses_its_close_fails_the_end_of_stdin(
    certs: EngineCerts, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = EnginePeer(answer_and_wait, tls=certs.server_context)
    attachment = await ExecAttachment.open(
        TcpEndpoint(url=f"https://127.0.0.1:{peer.port}", host="127.0.0.1", port=peer.port),
        "exec-1",
        ssl_context=certs.client_context,
    )

    def refuse() -> None:
        raise ssl.SSLError("the layer is in a state that cannot close")

    monkeypatch.setattr(cast(TlsCarrier, attachment._connected())._tls, "unwrap", refuse)

    with pytest.raises(SandboxError, match="cannot close stdin: the sandbox exec stream's TLS refused its close"):
        attachment.end_input()
    attachment.close()
    await asyncio.to_thread(peer.join)


# -- the endpoints the attachment dials --------------------------------------------------


async def test_a_missing_unix_socket_fails_the_attach(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await ExecAttachment.open(UnixSocketEndpoint(path=str(tmp_path / "none.sock")), "exec-1", ssl_context=None)


# -- the provider's door -------------------------------------------------------------------


async def test_the_provider_attaches_to_the_configured_engine() -> None:
    peer = EnginePeer(count_stdin)
    sandbox = DockerSandbox(settings=DockerSandboxSettings(host=f"tcp://127.0.0.1:{peer.port}", tls_verify=False))

    attachment = await sandbox._attach_exec("exec-1")
    attachment.end_input()

    assert await _read_all(attachment) == [SandboxStreamChunk(stream="stdout", data=b"0")]
    await asyncio.to_thread(peer.join)


async def test_the_provider_raises_an_engine_refusal_as_a_sandbox_error() -> None:
    body = json.dumps({"message": "No such exec instance"}).encode()
    peer = EnginePeer(refuse(404, body))
    sandbox = DockerSandbox(settings=DockerSandboxSettings(host=f"tcp://127.0.0.1:{peer.port}", tls_verify=False))

    with pytest.raises(SandboxError, match="No such exec instance"):
        await sandbox._attach_exec("exec-1")
    await asyncio.to_thread(peer.join)


async def test_the_provider_raises_an_untrusted_engine_as_unreachable(certs: EngineCerts) -> None:
    impostor = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    trustme.CA().issue_cert("127.0.0.1").configure_cert(impostor)
    peer = EnginePeer(count_stdin, tls=impostor)
    sandbox = DockerSandbox(settings=DockerSandboxSettings(host=f"tcp://127.0.0.1:{peer.port}"))
    sandbox._connection = (
        TcpEndpoint(url=f"https://127.0.0.1:{peer.port}", host="127.0.0.1", port=peer.port),
        certs.client_context,
    )
    sandbox._engine_verified = True

    with pytest.raises(SandboxError, match=r"cannot reach the Docker engine .*\(SSLCertVerificationError\)"):
        await sandbox._attach_exec("exec-1")

    assert not sandbox._engine_verified
    with pytest.raises(ssl.SSLError):
        await asyncio.to_thread(peer.join)


async def test_the_provider_raises_an_engine_refusing_the_client_identity_as_unreachable(certs: EngineCerts) -> None:
    port, finished = _engine_refusing_the_client_identity(certs)
    sandbox = DockerSandbox(settings=DockerSandboxSettings(host=f"tcp://127.0.0.1:{port}"))
    sandbox._connection = (
        TcpEndpoint(url=f"https://127.0.0.1:{port}", host="127.0.0.1", port=port),
        _without_client_identity(certs),
    )
    sandbox._engine_verified = True

    with pytest.raises(SandboxError, match=r"cannot reach the Docker engine .*\(SSLError\)") as raised:
        await sandbox._attach_exec("exec-1")

    assert isinstance(raised.value.__cause__, ssl.SSLError)
    assert "TLSV13_ALERT_CERTIFICATE_REQUIRED" in str(raised.value.__cause__)
    assert not sandbox._engine_verified
    await asyncio.to_thread(finished)
