"""Exec, interactive handle, file transfer, and the timeout→kill path (no engine)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import (
    SandboxError,
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_sandbox_docker import sessions
from tai42_sandbox_docker.provider import DockerSandbox, resolve_engine_url
from tai42_sandbox_docker.sessions import (
    DockerSandboxSession,
    engine_error,
    half_close_stdin,
    kill_exec_process,
    merge_env,
    read_exit_code,
    resolve_workspace_path,
)
from tai42_sandbox_docker.settings import DockerSandboxSettings

from .conftest import FakeContainer, FakeExec


def _session(container: FakeContainer, *, base_env=None) -> DockerSandboxSession:
    sandbox = DockerSandbox(docker=object(), settings=DockerSandboxSettings(host="tcp://engine:2376"))
    return DockerSandboxSession(
        sandbox=sandbox,
        session_id=container.id,
        container=container,
        workspace_key="ws1",
        durability="ephemeral",
        base_env=base_env or {},
    )


def _container() -> FakeContainer:
    return FakeContainer(container_id="c1", name=None, config={})


async def test_exec_runs_to_completion() -> None:
    container = _container()
    container.exec_frames = [(1, b"hello\n"), (2, b"warn\n")]
    container.exec_exit_code = 3
    session = _session(container)

    result = await session.exec(["echo", "hello"], timeout_seconds=5)

    assert result.exit_code == 3
    assert result.stdout == "hello\n"
    assert result.stderr == "warn\n"


async def test_exec_with_stdin_is_written_and_half_closed() -> None:
    container = _container()
    container.exec_frames = [(1, b"ok\n")]
    session = _session(container)

    await session.exec(["cat"], stdin=b"payload", timeout_seconds=5)

    stream = container.execs[0].stream
    assert stream is not None
    assert stream.written == [b"payload"]


async def test_exec_create_error_is_typed() -> None:
    container = _container()
    container.exec_raises = True
    session = _session(container)

    with pytest.raises(SandboxError, match="docker engine error"):
        await session.exec(["echo"], timeout_seconds=5)


async def test_exec_timeout_kills_the_process() -> None:
    container = _container()
    container.block_reads = True
    session = _session(container)

    with pytest.raises(SandboxExecTimeoutError):
        await session.exec(["sleep", "9"], timeout_seconds=0.1)

    assert any(e.cmd[:1] == ["kill"] for e in container.execs)


async def test_put_and_get_file_round_trip() -> None:
    container = _container()
    session = _session(container)

    await session.put_file("note.txt", b"bytes")
    assert await session.get_file("note.txt") == b"bytes"


async def test_put_file_creates_parent_dirs_for_nested_path() -> None:
    container = _container()
    session = _session(container)

    # A nested workspace path: the archive carries its parent-dir entries and is
    # extracted at the workspace root, so `sub/deep/` is created instead of docker's
    # put_archive 404-ing on the missing parent dir.
    await session.put_file("sub/deep/nested.txt", b"bytes")
    assert await session.get_file("sub/deep/nested.txt") == b"bytes"
    # The intermediate directories were materialized as archive members.
    assert "/workspace/sub" in container.files
    assert "/workspace/sub/deep" in container.files


async def test_get_missing_file_raises() -> None:
    container = _container()
    session = _session(container)
    with pytest.raises(SandboxError, match="not found"):
        await session.get_file("absent.txt")


async def test_interactive_stream_write_iterate_exit() -> None:
    container = _container()
    container.exec_frames = [(1, b"conf-ping\n")]
    session = _session(container)

    handle = await session.exec_start(["cat"], timeout_seconds=5)
    await handle.write_stdin(b"conf-ping\n")
    await handle.close_stdin()

    chunks: list[bytes] = []
    exit_code: int | None = None
    async for item in handle.output:
        if isinstance(item, SandboxStreamChunk):
            chunks.append(item.data)
        elif isinstance(item, SandboxStreamExit):
            exit_code = item.exit_code

    assert b"conf-ping\n" in b"".join(chunks)
    assert exit_code == 0
    stream = container.execs[0].stream
    assert stream is not None
    assert stream.written == [b"conf-ping\n"]

    # After exit: kill is a no-op and write raises a typed error.
    await handle.kill()
    with pytest.raises(SandboxError):
        await handle.write_stdin(b"late")


async def test_interactive_timeout_raises_on_iterator() -> None:
    container = _container()
    container.block_reads = True
    session = _session(container)

    handle = await session.exec_start(["cat"], timeout_seconds=0.1)
    with pytest.raises(SandboxExecTimeoutError):
        async for _ in handle.output:
            pass


async def test_interactive_immediate_deadline_kills_before_reading() -> None:
    container = _container()
    container.exec_frames = [(1, b"never read\n")]
    session = _session(container)

    # A non-positive timeout makes the very first ``remaining`` non-positive, so the
    # iterator kills and raises before ever reading a frame.
    handle = await session.exec_start(["cat"], timeout_seconds=0)
    with pytest.raises(SandboxExecTimeoutError):
        async for _ in handle.output:
            pass

    assert any(e.cmd[:1] == ["kill"] for e in container.execs)


async def test_interactive_stream_surfaces_stderr() -> None:
    container = _container()
    container.exec_frames = [(2, b"boom on stderr\n")]
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=5)
    chunks: list[SandboxStreamChunk] = []
    async for item in handle.output:
        if isinstance(item, SandboxStreamChunk):
            chunks.append(item)

    # A stream-id-2 frame is surfaced as a stderr chunk, not stdout.
    assert [(c.stream, c.data) for c in chunks] == [("stderr", b"boom on stderr\n")]


async def test_put_file_engine_error_is_typed() -> None:
    from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]

    container = _container()

    async def _boom(path: str, data: bytes) -> None:
        raise DockerError(500, "archive rejected")

    container.put_archive = _boom  # type: ignore[method-assign]
    session = _session(container)
    with pytest.raises(SandboxError, match="docker engine error"):
        await session.put_file("note.txt", b"bytes")


def test_resolve_workspace_path() -> None:
    assert resolve_workspace_path("sub/nested.txt") == "/workspace/sub/nested.txt"
    assert resolve_workspace_path("/etc/passwd") == "/etc/passwd"  # absolute is caller's own
    with pytest.raises(SandboxError, match="escapes"):
        resolve_workspace_path("../etc/passwd")


def test_merge_env_unwraps_and_overlays() -> None:
    base = {"A": SecretStr("1"), "B": SecretStr("2")}
    overlay = {"B": SecretStr("9"), "C": SecretStr("3")}
    assert merge_env(base, overlay) == {"A": "1", "B": "9", "C": "3"}
    assert merge_env(base, None) == {"A": "1", "B": "2"}


def test_engine_error_carries_status_and_message() -> None:
    from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]

    wrapped = engine_error(DockerError(500, "boom"))
    assert isinstance(wrapped, SandboxError)
    assert "[500]" in str(wrapped)
    assert "boom" in str(wrapped)


async def test_kill_exec_process_signals_then_falls_back() -> None:
    # Signal path: a live pid is killed via a sibling exec, no container kill.
    container = _container()
    await kill_exec_process(container, 123)
    assert any(e.cmd == ["kill", "-KILL", "123"] for e in container.execs)
    assert container.killed_signals == []

    # Fallback: the sibling exec cannot be created, so the container is killed.
    unsignalable = _container()
    unsignalable.exec_raises = True
    await kill_exec_process(unsignalable, 123)
    assert unsignalable.killed_signals == ["SIGKILL"]

    # No pid at all: straight to the container-kill last resort.
    no_pid = _container()
    await kill_exec_process(no_pid, None)
    assert no_pid.killed_signals == ["SIGKILL"]


async def test_read_exit_code_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "_EXIT_CODE_POLLS", 2)
    monkeypatch.setattr(sessions, "_EXIT_CODE_POLL_INTERVAL", 0)
    exec_obj = FakeExec(container=_container(), cmd=["x"], frames=[], exit_code=0)
    exec_obj.running = True  # never reports an exit
    with pytest.raises(SandboxError, match="without reporting an exit code"):
        await read_exit_code(exec_obj)


class _Sock:
    def __init__(self, *, raises: bool = False) -> None:
        self.shutdown_how: int | None = None
        self._raises = raises

    def shutdown(self, how: int) -> None:
        if self._raises:
            raise OSError("socket already shut")
        self.shutdown_how = how


class _Transport:
    def __init__(self, *, can: bool, sock: _Sock | None = None) -> None:
        self._can = can
        self._sock = sock
        self.wrote_eof = False

    def can_write_eof(self) -> bool:
        return self._can

    def write_eof(self) -> None:
        self.wrote_eof = True

    def get_extra_info(self, name: str):
        return self._sock if name == "socket" else None


class _Conn:
    def __init__(self, transport) -> None:
        self.transport = transport


class _Resp:
    def __init__(self, connection) -> None:
        self.connection = connection


class _Stream:
    def __init__(self, resp) -> None:
        self._resp = resp

    async def _init(self) -> None:
        return None


async def test_half_close_stdin_write_eofs_a_plain_transport() -> None:
    # Plain TCP: a transport that CAN write-EOF gets the clean FIN half-close, and the
    # socket is never touched.
    sock = _Sock()
    transport = _Transport(can=True, sock=sock)
    await half_close_stdin(_Stream(_Resp(_Conn(transport))))
    assert transport.wrote_eof is True
    assert sock.shutdown_how is None


async def test_half_close_stdin_shuts_socket_write_half_over_tls() -> None:
    import socket as _socket

    # mTLS: an SSL transport reports can_write_eof()==False, so EOF is signalled by
    # shutting the WRITE half of the underlying socket (SHUT_WR) — this is the fix
    # that stops cat-style execs hanging over the TLS control API.
    sock = _Sock()
    transport = _Transport(can=False, sock=sock)
    await half_close_stdin(_Stream(_Resp(_Conn(transport))))
    assert transport.wrote_eof is False
    assert sock.shutdown_how == _socket.SHUT_WR


async def test_half_close_stdin_is_a_safe_noop_without_a_socket() -> None:
    # No response/connection yet, and an SSL transport whose socket is gone or already
    # shut: never raises, never write-EOFs.
    await half_close_stdin(_Stream(None))
    await half_close_stdin(_Stream(_Resp(_Conn(_Transport(can=False, sock=None)))))
    already_shut = _Sock(raises=True)
    await half_close_stdin(_Stream(_Resp(_Conn(_Transport(can=False, sock=already_shut)))))


def test_resolve_engine_url_normalizes_tls_scheme() -> None:
    # Under mTLS (tls=True), a tcp:// / http:// control endpoint is normalized to
    # https:// so aiodocker actually runs TLS over the caller-supplied ssl_context
    # (otherwise it dials plaintext against the TLS port).
    assert resolve_engine_url("tcp://engine:2376", tls=True) == "https://engine:2376"
    assert resolve_engine_url("http://engine:2376", tls=True) == "https://engine:2376"
    assert resolve_engine_url("https://engine:2376", tls=True) == "https://engine:2376"
    # Without TLS the scheme is left untouched for aiodocker's own handling.
    assert resolve_engine_url("tcp://engine:2376", tls=False) == "tcp://engine:2376"
    # Local sockets pass through (a bare path becomes a unix socket) regardless of tls.
    assert resolve_engine_url("unix:///var/run/docker.sock", tls=True) == "unix:///var/run/docker.sock"
    assert resolve_engine_url("npipe:////./pipe/docker_engine", tls=False) == "npipe:////./pipe/docker_engine"
    assert resolve_engine_url("/var/run/docker.sock", tls=True) == "unix:///var/run/docker.sock"


def test_create_engine_dials_https_under_mtls(monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end through _create_engine: a tcp:// host + tls_verify builds an SSL
    # context and hands aiodocker an https:// URL (not tcp://).
    import ssl as _ssl

    captured: dict[str, object] = {}

    def _fake_docker(*, url: str, ssl_context: object) -> object:
        captured["url"] = url
        captured["ssl_context"] = ssl_context
        return object()

    monkeypatch.setattr("tai42_sandbox_docker.provider.Docker", _fake_docker)
    sandbox = DockerSandbox(
        docker=object(),
        settings=DockerSandboxSettings(host="tcp://engine:2376"),
    )
    monkeypatch.setattr(sandbox, "_build_ssl_context", lambda _s: _ssl.create_default_context())

    sandbox._create_engine()

    assert captured["url"] == "https://engine:2376"
    assert captured["ssl_context"] is not None
