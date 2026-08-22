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
from tai42_sandbox_docker.provider import DockerSandbox
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


async def test_half_close_stdin_write_eofs_the_transport() -> None:
    class _Transport:
        def __init__(self, *, can: bool) -> None:
            self._can = can
            self.wrote_eof = False

        def can_write_eof(self) -> bool:
            return self._can

        def write_eof(self) -> None:
            self.wrote_eof = True

    class _Conn:
        def __init__(self, transport) -> None:
            self.transport = transport

    class _Resp:
        def __init__(self, connection) -> None:
            self.connection = connection

    class _Stream:
        def __init__(self, resp) -> None:
            self._resp = resp

    transport = _Transport(can=True)
    await half_close_stdin(_Stream(_Resp(_Conn(transport))))
    assert transport.wrote_eof is True

    # No response yet, or a transport that cannot half-close: a safe no-op.
    await half_close_stdin(_Stream(None))
    idle = _Transport(can=False)
    await half_close_stdin(_Stream(_Resp(_Conn(idle))))
    assert idle.wrote_eof is False
