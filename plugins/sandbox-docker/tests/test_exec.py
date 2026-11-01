"""Exec, interactive handle, file transfer, and the timeout→kill path (no engine)."""

from __future__ import annotations

import asyncio
from typing import Any

import anyio
import pytest
from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from pydantic import SecretStr
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_kit.sandbox import exec_kill
from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE, KillOutcome, KillResult

from tai42_sandbox_docker import sessions
from tai42_sandbox_docker.attach import ExecAttachment
from tai42_sandbox_docker.engine_endpoint import TcpEndpoint
from tai42_sandbox_docker.exec_control import EXEC_WRAPPER, kill_exec_group
from tai42_sandbox_docker.exec_status import read_exit_code
from tai42_sandbox_docker.provider import DockerSandbox
from tai42_sandbox_docker.sessions import (
    DockerSandboxSession,
    engine_error,
    merge_env,
)
from tai42_sandbox_docker.settings import DockerSandboxSettings

from .attach.engine_peer import EnginePeer, PeerConnection
from .conftest import FakeAttachment, FakeContainer, FakeExec, is_kill_exec, wrapped_exec_id

# The loggers an exec and its kill report on: the provider's own and the kit's.
_EXEC_LOGGERS = ("tai42_sandbox_docker", "tai42_kit")


def _session(container: FakeContainer, *, base_env=None) -> DockerSandboxSession:
    sandbox = DockerSandbox(
        docker=object(), attach=container.attach_exec, settings=DockerSandboxSettings(host="tcp://engine:2376")
    )
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
    # The argv runs under the pidfile wrapper, named by the exec's own id.
    assert wrapped_exec_id(container.execs[0].cmd) is not None
    assert container.execs[0].cmd[4:] == ["echo", "hello"]


async def test_exec_with_stdin_is_written_and_ended() -> None:
    container = _container()
    container.exec_frames = [(1, b"ok\n")]
    session = _session(container)

    await session.exec(["cat"], stdin=b"payload", timeout_seconds=5)

    attachment = container.execs[0].attachment
    assert attachment is not None
    assert attachment.written == [b"payload"]
    assert attachment.input_ended


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

    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert killer.cmd[3] == wrapped_exec_id(target.cmd)
    assert target.killed is True


async def test_a_cancelled_one_shot_exec_kills_its_process() -> None:
    container = _container()
    container.block_reads = True
    session = _session(container)

    call = asyncio.ensure_future(session.exec(["sleep", "9"], timeout_seconds=30))
    while not container.execs or not container.execs[0].started:
        await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert target.killed is True


async def test_a_one_shot_exec_cancelled_once_its_container_is_gone_notes_no_kill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = _container()
    container.block_reads = True
    session = _session(container)

    call = asyncio.ensure_future(session.exec(["sleep", "9"], timeout_seconds=30))
    while not container.execs or not container.execs[0].started:
        await asyncio.sleep(0)
    container.deleted = True
    call.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await call

    # The exec ended with its container: there was nothing to kill, and nothing to report.
    assert getattr(raised.value, "__notes__", []) == []
    assert not [r for r in caplog.records if r.name.startswith(_EXEC_LOGGERS)]


async def test_reading_the_output_after_the_session_is_destroyed_raises_a_sandbox_error() -> None:
    # The engine forgets the exec with its container: its exit code cannot be read.
    container = _container()
    session = _session(container)
    handle = await session.exec_start(["cat"], timeout_seconds=30)
    container.deleted = True
    await handle.kill()
    with pytest.raises(SandboxError, match=r"docker engine error \[404\]") as raised:
        await _drain(handle)
    assert isinstance(raised.value.__cause__, DockerError)


async def test_a_one_shot_exec_whose_attach_fails_raises_a_sandbox_error() -> None:
    container = _container()
    session = _session(container)
    container.attach_error = DockerError(500, "attach failed")
    with pytest.raises(SandboxError, match=r"docker engine error \[500\]: attach failed"):
        await session.exec(["true"], timeout_seconds=5)


async def test_a_one_shot_exec_whose_attach_fails_before_it_starts_raises_without_a_kill_note(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The engine never started the exec: the cleanup kill finds nothing running, the exec's own
    # inspect shows it never started, and the kill's mark keeps its command from ever starting,
    # so no note claims it may still be running.
    container = _container()
    session = _session(container)
    container.attach_error = DockerError(500, "attach failed")
    with pytest.raises(SandboxError, match=r"docker engine error \[500\]: attach failed") as raised:
        await session.exec(["true"], timeout_seconds=5)
    assert getattr(raised.value, "__notes__", []) == []
    assert not [r for r in caplog.records if r.name.startswith(_EXEC_LOGGERS)]
    assert [is_kill_exec(exec_obj.cmd) for exec_obj in container.execs] == [False, True]


async def test_a_one_shot_exec_the_engine_never_started_whose_kill_fails_raises_without_a_kill_note(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The kill fails, yet the exec's own inspect shows the engine never started it: the kill's
    # mark keeps its command from ever starting, so nothing was left to stop.
    container = _container()
    session = _session(container)
    container.attach_error = DockerError(500, "attach failed")
    container.kill_failure = b"sh: can't create /tmp/tai-exec.x.kill: Read-only file system\n"
    with pytest.raises(SandboxError, match=r"docker engine error \[500\]: attach failed") as raised:
        await session.exec(["true"], timeout_seconds=5)
    assert getattr(raised.value, "__notes__", []) == []
    assert not [r for r in caplog.records if r.name.startswith(_EXEC_LOGGERS)]


async def test_an_interactive_start_whose_attach_fails_before_it_starts_raises_without_a_kill_note(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = _container()
    session = _session(container)
    container.attach_error = DockerError(500, "attach failed")
    with pytest.raises(SandboxError, match=r"docker engine error \[500\]: attach failed") as raised:
        await session.exec_start(["cat"], timeout_seconds=5)
    assert getattr(raised.value, "__notes__", []) == []
    assert not [r for r in caplog.records if r.name.startswith(_EXEC_LOGGERS)]
    assert [is_kill_exec(exec_obj.cmd) for exec_obj in container.execs] == [False, True]


async def test_a_one_shot_exec_whose_exit_code_cannot_be_read_raises_a_sandbox_error_without_a_kill_note(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The exec has ended when its exit code read fails; the cleanup kill finds nothing running
    # and the exec's own inspect, answering again, shows it ended: no note claims it may still
    # be running.
    container = _container()
    container.inspect_failures = 1
    session = _session(container)
    with pytest.raises(SandboxError, match=r"docker engine error \[500\]: exec inspect failed") as raised:
        await session.exec(["true"], timeout_seconds=5)
    assert getattr(raised.value, "__notes__", []) == []
    assert not [r for r in caplog.records if r.name.startswith(_EXEC_LOGGERS)]


async def test_kill_after_the_container_stopped_is_a_no_op() -> None:
    # A stopped container refuses every new exec with a 409, and its exec has ended with it.
    container = _container()
    session = _session(container)
    handle = await session.exec_start(["cat"], timeout_seconds=30)
    target = container.execs[0]
    target.running = False
    target._exit_code = 137
    container.exec_error = 409
    await handle.kill()
    await handle.kill()
    # The stream stays open for the exec's unread tail and its own exit code.
    assert await _drain(handle) == [SandboxStreamChunk(stream="stdout", data=b""), SandboxStreamExit(exit_code=137)]


async def test_an_exec_ended_within_the_deadline_gives_its_output_and_exit_however_late_it_is_read() -> None:
    container = _container()
    container.exec_frames = [(1, b"done")]
    container.exec_exit_code = 3
    session = _session(container)
    handle = await session.exec_start(["sh", "-c", "printf done; exit 3"], timeout_seconds=0.05)
    (command,) = container.execs
    assert command.attachment is not None
    # The exec ends at once, its output sent; the output is first read well after its deadline.
    command.running = False
    command.attachment.engine_ends()
    await asyncio.sleep(0.15)
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"done"),
        SandboxStreamExit(exit_code=3),
    ]
    assert not command.killed


async def test_an_exec_ended_within_the_deadline_whose_stream_is_still_open_gives_its_output_and_exit() -> None:
    # The engine still holds output the provider has not read, as when the attachment paused its
    # reading on a full buffer: the stream stays open past the deadline though the exec ended.
    container = _container()
    container.exec_frames = [(1, b"a" * 4096), (1, b"b" * 4096)]
    container.exec_exit_code = 3
    session = _session(container)
    handle = await session.exec_start(["sh"], timeout_seconds=0.05)
    (command,) = container.execs
    command.running = False
    await asyncio.sleep(0.15)
    assert await asyncio.wait_for(_drain(handle), 5) == [
        SandboxStreamChunk(stream="stdout", data=b"a" * 4096),
        SandboxStreamChunk(stream="stdout", data=b"b" * 4096),
        SandboxStreamExit(exit_code=3),
    ]
    assert not command.killed


async def test_an_exec_whose_stream_is_lost_before_its_deadline_is_killed_at_it_and_times_out() -> None:
    container = _container()
    container.exec_frames = [(1, b"partial")]
    session = _session(container)
    handle = await session.exec_start(["sleep", "100"], timeout_seconds=0.05)
    (command,) = container.execs
    assert command.attachment is not None
    # The engine connection is lost while the command runs on; nothing reads the output.
    command.attachment.engine_ends()
    await asyncio.sleep(0.15)
    # The deadline killed the exec whether or not anything read its output.
    assert command.killed
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (len(b"partial"), 0)


async def test_a_stop_after_the_output_ended_reports_the_exec_ended_without_a_kill() -> None:
    container = _container()
    session = _session(container)
    handle = await session.exec_start(["true"], timeout_seconds=5)
    await _drain(handle)
    assert await handle._stop() == KillResult(KillOutcome.EXEC_ENDED)
    assert not [exec_obj for exec_obj in container.execs if is_kill_exec(exec_obj.cmd)]


async def test_kill_in_a_paused_container_raises() -> None:
    # A paused container refuses the kill with a 409 too, yet its exec is still running.
    container = _container()
    session = _session(container)
    handle = await session.exec_start(["cat"], timeout_seconds=30)
    container.exec_error = 409
    with pytest.raises(SandboxError, match=r"docker engine error \[409\]"):
        await handle.kill()
    assert handle._stopped is False


async def test_kill_whose_follow_up_inspect_fails_raises_the_kill_error() -> None:
    container = _container()
    session = _session(container)
    handle = await session.exec_start(["cat"], timeout_seconds=30)
    container.exec_error = 409
    container.execs[0].inspect_raises = True
    with pytest.raises(SandboxError, match=r"docker engine error \[409\]") as raised:
        await handle.kill()
    assert raised.value.__notes__ == [
        "the inspect of the sandbox exec also failed: docker engine error [500]: exec inspect failed"
    ]


async def test_a_kill_the_engine_never_answers_fails_once_its_time_limit_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exec_kill, "KILL_TIMEOUT_SECONDS", 0.05)
    container = _container()
    container.kill_round_trip = 3600  # the engine accepts the kill and never answers
    session = _session(container)
    handle = await session.exec_start(["cat"], timeout_seconds=30)
    async with asyncio.timeout(2):
        with pytest.raises(SandboxError, match=r"sandbox exec kill did not finish within 0.05s"):
            await handle.kill()
    assert handle._stopped is False


async def test_a_one_shot_exec_cancelled_twice_while_its_kill_hangs_still_ends(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(exec_kill, "KILL_TIMEOUT_SECONDS", 0.05)
    container = _container()
    container.block_reads = True
    container.kill_round_trip = 3600  # the engine accepts the kill and never answers
    session = _session(container)
    call = asyncio.ensure_future(session.exec(["sleep", "9"], timeout_seconds=30))
    while not container.execs or not container.execs[0].started:
        await asyncio.sleep(0)
    call.cancel("first")
    await container.kill_requested.wait()
    call.cancel("second")
    async with asyncio.timeout(2):
        with pytest.raises(asyncio.CancelledError) as raised:
            await call
    assert raised.value.args == ("first",)
    assert raised.value.__notes__ == [
        "the kill of the sandbox exec also failed: sandbox exec kill did not finish within 0.05s"
    ]
    assert [r.levelname for r in caplog.records if r.name.startswith(_EXEC_LOGGERS)] == ["ERROR"]


# A real engine takes a round trip to run the cleanup kill, so a caller's cancellation can
# land inside it: an anyio scope re-delivers its cancellation at every wake-up, and a task
# can be cancelled twice. The kill still runs to its end, so the command stops.
_KILL_ROUND_TRIP = 0.05


async def test_a_one_shot_exec_cancelled_by_an_anyio_scope_still_kills_its_process() -> None:
    container = _container()
    container.block_reads = True
    container.kill_round_trip = _KILL_ROUND_TRIP
    session = _session(container)

    with anyio.move_on_after(0.05) as scope:
        await session.exec(["sleep", "9"], timeout_seconds=30)

    assert scope.cancelled_caught
    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert target.killed is True


async def test_a_one_shot_exec_cancelled_again_and_again_still_kills_its_process_and_raises_the_first_cancel() -> None:
    container = _container()
    container.block_reads = True
    container.kill_round_trip = _KILL_ROUND_TRIP
    session = _session(container)
    call = asyncio.ensure_future(session.exec(["sleep", "9"], timeout_seconds=30))
    while not container.execs or not container.execs[0].started:
        await asyncio.sleep(0)
    call.cancel("first")
    await container.kill_requested.wait()
    call.cancel("second")
    # A third cancel lands while the call already waits out its kill.
    await asyncio.sleep(_KILL_ROUND_TRIP / 5)
    call.cancel("third")
    with pytest.raises(asyncio.CancelledError) as raised:
        await call

    target, _killer = container.execs
    assert target.killed is True
    # The call ends with its own first cancellation, never one that came during its kill.
    assert raised.value.args == ("first",)


async def test_a_one_shot_exec_cancelled_while_its_timeout_kill_runs_kills_it_and_raises_the_cancel() -> None:
    container = _container()
    container.block_reads = True
    container.kill_round_trip = _KILL_ROUND_TRIP
    session = _session(container)

    call = asyncio.ensure_future(session.exec(["sleep", "9"], timeout_seconds=0.05))
    await container.kill_requested.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await call

    target, _killer = container.execs
    assert target.killed is True
    # The timeout the kill was cleaning up after stays on the cancellation, as its cause.
    assert isinstance(raised.value.__cause__, SandboxExecTimeoutError)


async def test_an_exec_start_cancelled_by_an_anyio_scope_while_it_attaches_kills_the_started_exec() -> None:
    container = _container()
    container.kill_round_trip = _KILL_ROUND_TRIP
    session = _session(container)
    container.attach_hook = _hang
    with anyio.move_on_after(0.05) as scope:
        await session.exec_start(["sleep", "9"], timeout_seconds=30)

    assert scope.cancelled_caught
    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert target.killed is True


async def test_an_exec_start_whose_attach_is_never_answered_times_out_and_kills_the_started_exec() -> None:
    container = _container()
    session = _session(container)
    container.attach_hook = _hang

    with pytest.raises(SandboxExecTimeoutError) as raised:
        # The outer bound only keeps a broken deadline from hanging the test.
        await asyncio.wait_for(session.exec_start(["sleep", "9"], timeout_seconds=0.1), 5)

    assert (raised.value.timeout_seconds, raised.value.stdout_len, raised.value.stderr_len) == (0.1, 0, 0)
    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert target.killed is True


async def test_an_exec_start_cancelled_in_the_same_tick_its_deadline_passes_raises_the_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _container()
    session = _session(container)
    loop = asyncio.get_running_loop()
    deadlines: list[float] = []
    timeout_at = asyncio.timeout_at

    def recording_timeout_at(when: float | None) -> asyncio.Timeout:
        assert when is not None
        deadlines.append(when)
        return timeout_at(when)

    monkeypatch.setattr(asyncio, "timeout_at", recording_timeout_at)
    start = asyncio.ensure_future(session.exec_start(["sleep", "9"], timeout_seconds=0.1))

    async def cancel_at_the_deadline(attachment: FakeAttachment) -> None:
        # The caller's cancel is timed for the deadline's own loop time, so both reach the
        # starting task in the one tick before it runs again.
        (deadline,) = deadlines
        loop.call_at(deadline, start.cancel)
        await asyncio.Event().wait()

    container.attach_hook = cancel_at_the_deadline

    with pytest.raises(asyncio.CancelledError):
        # The outer bound only keeps a broken deadline from hanging the test.
        await asyncio.wait_for(start, 5)

    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert target.killed is True


async def test_an_exec_start_whose_attach_raises_a_timeout_of_its_own_raises_an_engine_error_and_kills_it() -> None:
    container = _container()
    session = _session(container)

    async def timed_out_dial(attachment: FakeAttachment) -> None:
        raise TimeoutError("connect timed out")

    container.attach_hook = timed_out_dial

    with pytest.raises(SandboxError, match="cannot reach the Docker engine") as raised:
        await session.exec_start(["sleep", "9"], timeout_seconds=30)

    # The attach's own timeout, well before the exec's deadline, is the engine being unreachable.
    assert not isinstance(raised.value, SandboxExecTimeoutError)
    assert isinstance(raised.value.__cause__, TimeoutError)
    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert target.killed is True


def _peer_session(container: FakeContainer, peer: EnginePeer) -> DockerSandboxSession:
    """A session whose commands' execs attach to ``peer`` over a real attachment, their sibling kills to the fake."""

    async def attach(exec_id: str) -> ExecAttachment:
        exec_obj = next(e for e in container.execs if e.id == exec_id)
        if is_kill_exec(exec_obj.cmd):
            return await container.attach_exec(exec_id)
        exec_obj.started = True
        return await ExecAttachment.open(
            TcpEndpoint(url=f"tcp://127.0.0.1:{peer.port}", host="127.0.0.1", port=peer.port), exec_id, ssl_context=None
        )

    sandbox = DockerSandbox(docker=object(), attach=attach, settings=DockerSandboxSettings(host="tcp://engine:2376"))
    return DockerSandboxSession(
        sandbox=sandbox, session_id="c1", container=container, workspace_key="ws1", durability="ephemeral", base_env={}
    )


async def test_an_exec_start_whose_engine_takes_the_connection_but_never_answers_times_out_and_kills_it() -> None:
    peer = EnginePeer(PeerConnection.wait_for_client_close)
    container = _container()
    session = _peer_session(container, peer)

    with pytest.raises(SandboxExecTimeoutError) as raised:
        # The outer bound only keeps a broken deadline from hanging the test.
        await asyncio.wait_for(session.exec_start(["sleep", "100"], timeout_seconds=0.5), 5)

    assert (raised.value.timeout_seconds, raised.value.stdout_len, raised.value.stderr_len) == (0.5, 0, 0)
    assert container.kill_requested.is_set()
    # The peer's wait ends only once the attach connection is gone.
    await asyncio.to_thread(peer.join)


async def test_an_exec_start_has_one_deadline_its_slow_attach_and_its_output_share() -> None:
    container = _container()
    container.block_reads = True
    session = _session(container)

    async def slow_answer(attachment: FakeAttachment) -> None:
        await asyncio.sleep(0.6)

    container.attach_hook = slow_answer
    loop = asyncio.get_running_loop()
    started = loop.time()
    handle = await session.exec_start(["sleep", "9"], timeout_seconds=1.0)
    with pytest.raises(SandboxExecTimeoutError):
        await _drain(handle)

    # The output gets what the attach left of the deadline, never a whole timeout of its own.
    assert loop.time() - started < 1.5


# A failed cleanup kill never replaces the call's own failure: the caller still sees that
# failure, carrying a note of the failed kill, and the kill's error is logged at ERROR.
_KILL_FAILED_NOTE = "the kill of the sandbox exec also failed: sandbox exec kill failed"


def _assert_the_failed_kill_was_kept(exc: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    assert any(note.startswith(_KILL_FAILED_NOTE) for note in exc.__notes__)
    [record] = [record for record in caplog.records if record.levelname == "ERROR"]
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], SandboxError)


async def test_a_cancelled_one_shot_exec_whose_kill_fails_still_raises_the_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = _container()
    container.block_reads = True
    container.kill_failure = b"sh: can't kill pid -7: Operation not permitted\n"
    session = _session(container)

    call = asyncio.ensure_future(session.exec(["sleep", "9"], timeout_seconds=30))
    while not container.execs or not container.execs[0].started:
        await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await call
    _assert_the_failed_kill_was_kept(raised.value, caplog)


async def test_a_one_shot_exec_under_a_timeout_scope_whose_kill_fails_still_times_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = _container()
    container.block_reads = True
    container.kill_failure = b"sh: can't kill pid -7: Operation not permitted\n"
    session = _session(container)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await session.exec(["sleep", "9"], timeout_seconds=30)
    [record] = [record for record in caplog.records if record.levelname == "ERROR"]
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], SandboxError)


async def test_a_one_shot_exec_past_its_timeout_whose_kill_fails_still_raises_the_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = _container()
    container.block_reads = True
    container.kill_failure = b"sh: can't kill pid -7: Operation not permitted\n"
    session = _session(container)

    with pytest.raises(SandboxExecTimeoutError) as raised:
        await session.exec(["sleep", "9"], timeout_seconds=0.05)
    _assert_the_failed_kill_was_kept(raised.value, caplog)


async def test_an_interactive_exec_past_its_deadline_whose_kill_fails_still_raises_the_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = _container()
    container.block_reads = True
    container.kill_failure = b"sh: can't kill pid -7: Operation not permitted\n"
    session = _session(container)

    handle = await session.exec_start(["sleep", "9"], timeout_seconds=0.1)
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await _drain(handle)
    _assert_the_failed_kill_was_kept(raised.value, caplog)


# The kill's own failure when its scan cannot read the open files of a process the command left.
_UNREADABLE_HOLDER = b"cannot read the open files of process 19: ls: /proc/19/fd: Permission denied\n"


async def test_a_kill_whose_scan_fails_raises_its_failure_though_the_exec_has_ended() -> None:
    # The exec's end says nothing of a process the command left holding its output, which the
    # kill could not tell: the kill's failure stands.
    container = _container()
    container.kill_failure = _UNREADABLE_HOLDER
    target = await container.exec(cmd=["sh", "-c", EXEC_WRAPPER, "x1", "cat"])
    target.started = True
    target.running = False
    with pytest.raises(SandboxError, match=r"^sandbox exec kill failed: cannot read the open files of process 19"):
        await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)


async def test_a_deadline_kill_whose_scan_fails_on_an_exec_that_has_ended_times_out_and_notes_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = _container()
    container.block_reads = True
    container.kill_failure = _UNREADABLE_HOLDER
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=0.05)
    container.execs[0].running = False  # the command ended, a process it started left behind
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    _assert_the_failed_kill_was_kept(raised.value, caplog)
    assert "cannot read the open files of process 19" in raised.value.__notes__[0]


async def test_a_callers_kill_whose_scan_fails_on_an_exec_that_has_ended_raises_it() -> None:
    container = _container()
    container.block_reads = True
    container.kill_failure = _UNREADABLE_HOLDER
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=30)
    container.execs[0].running = False
    with pytest.raises(SandboxError, match=r"^sandbox exec kill failed: cannot read the open files of process 19"):
        await handle.kill()
    assert handle._stopped is False


async def _hang(attachment: FakeAttachment) -> None:
    """An attach whose exec the engine started but whose answer never comes."""
    await asyncio.Event().wait()


async def test_a_one_shot_exec_whose_connection_is_lost_under_its_stdin_write_while_it_runs_raises_and_kills_it() -> (
    None
):
    container = _container()
    # The connection is lost while the cat still runs: the stream ends with no exit code recorded.
    container.attach_drops = True
    container.stdin_ends_stream = True
    session = _session(container)

    with pytest.raises(SandboxError, match=r"^sandbox exec stream ended, but the engine reported no exit code"):
        await session.exec(["cat"], stdin=b"payload", timeout_seconds=30)

    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert target.killed is True


async def test_a_one_shot_exec_whose_connection_the_engine_drops_under_its_stdin_write_returns_its_result() -> None:
    container = _container()
    container.exec_frames = [(1, b"p")]
    session = _session(container)

    # The command exited without reading its stdin and the engine dropped the connection.
    container.stdin_ends_stream = True
    result = await session.exec(["head", "-c", "1"], stdin=b"payload", timeout_seconds=30)
    assert result == ExecResult(exit_code=0, stdout="p", stderr="")
    assert [is_kill_exec(exec_obj.cmd) for exec_obj in container.execs] == [False]


async def test_a_one_shot_exec_whose_attach_connection_the_engine_released_returns_its_result() -> None:
    container = _container()
    container.exec_frames = [(1, b"p")]
    session = _session(container)

    async def _release(attachment: FakeAttachment) -> None:
        # The command exited and the engine closed the attach connection.
        attachment.engine_ends()

    container.attach_hook = _release
    result = await session.exec(["head", "-c", "1"], stdin=b"payload", timeout_seconds=30)
    assert result == ExecResult(exit_code=0, stdout="p", stderr="")
    target = container.execs[0]
    assert target.attachment is not None
    assert target.attachment.written == []


async def test_a_one_shot_exec_whose_output_the_engine_ends_under_its_stdin_write_returns_its_result() -> None:
    container = _container()
    container.exec_frames = [(1, b"y")]
    # A stdin write still buffered when the command exits: the engine ends the output.
    container.stdin_ends_stream = True
    session = _session(container)

    result = await session.exec(["head", "-c", "1"], stdin=b"y" * 2_000_000, timeout_seconds=30)
    assert result == ExecResult(exit_code=0, stdout="y", stderr="")
    attachment = container.execs[0].attachment
    assert attachment is not None
    assert attachment.written == []  # the stdin the command never took is dropped
    assert not attachment.input_ended  # nothing ends the stdin of a stream that has ended
    assert attachment.closed.is_set()


async def test_an_interactive_write_whose_output_the_engine_ends_raises_and_the_output_ends_with_the_exit_code() -> (
    None
):
    container = _container()
    container.exec_frames = [(1, b"y")]
    session = _session(container)

    handle = await session.exec_start(["head", "-c", "1"], timeout_seconds=30)
    container.stdin_ends_stream = True
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
        await handle.write_stdin(b"y" * 2_000_000)
    attachment = container.execs[0].attachment
    assert attachment is not None
    assert attachment.written == []
    assert await _drain(handle) == [
        SandboxStreamChunk(stream="stdout", data=b"y"),
        SandboxStreamExit(exit_code=0),
    ]


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
    stream = container.execs[0].attachment
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


async def test_an_interactive_timeout_counts_the_output_the_attachment_held_when_its_kill_closed_it() -> None:
    container = _container()
    container.block_reads = True
    container.received = [
        SandboxStreamChunk(stream="stdout", data=b"held\n"),
        SandboxStreamChunk(stream="stderr", data=b"e"),
    ]
    session = _session(container)

    handle = await session.exec_start(["cat"], timeout_seconds=0.1)
    await asyncio.sleep(0.3)  # nothing reads: the deadline's kill closes the stream
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await _drain(handle)

    assert (raised.value.stdout_len, raised.value.stderr_len) == (5, 1)


async def test_a_deadline_kill_whose_stream_stays_open_notes_the_count_may_miss_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions.DockerSandboxExecHandle, "_status_grace_seconds", 0.1)
    container = _container()
    container.block_reads = True
    container.killed_stream_stays_open = True  # the engine is slow to end the stream
    session = _session(container)

    handle = await session.exec_start(["sleep", "9"], timeout_seconds=0.05)
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 5)
    assert raised.value.__notes__ == [
        "the timeout's count may miss output: the sandbox exec stream did not end within 0.1s of the kill"
    ]
    (command,) = [exec_obj for exec_obj in container.execs if not is_kill_exec(exec_obj.cmd)]
    assert command.killed


async def test_an_exec_killed_past_its_deadline_ends_with_its_exit() -> None:
    container = _container()
    container.exec_frames = [(1, b"ready\n"), (1, b"last\n")]
    session = _session(container)

    handle = await session.exec_start(["cat"], timeout_seconds=0.1)
    output = handle.output
    first = await anext(output)
    await asyncio.sleep(0.2)
    await handle.kill()
    rest = [item async for item in output]

    # The deadline's kill counted what the stream carried up to its end; the caller's kill wins
    # over the timeout: the output yields what the stream had received, then the killed status.
    assert first == SandboxStreamChunk(stream="stdout", data=b"ready\n")
    assert rest == [SandboxStreamChunk(stream="stdout", data=b"last\n"), SandboxStreamExit(exit_code=137)]


async def test_a_killed_exec_whose_stream_stays_open_ends_at_once() -> None:
    container = _container()
    container.block_reads = True  # the attach stream never ends on its own
    session = _session(container)

    handle = await session.exec_start(["cat"], timeout_seconds=30)
    reader = asyncio.ensure_future(_drain(handle))
    await asyncio.sleep(0)
    await handle.kill()
    async with asyncio.timeout(1):
        items = await reader

    assert items == [SandboxStreamExit(exit_code=137)]
    assert container.execs[0].killed is True


async def test_a_kill_that_races_the_commands_own_exit_ends_with_the_commands_real_status() -> None:
    # The command exits 0 by itself just as the kill signals the group its unreaped leader
    # still holds: the kill reports it signalled, and the output carries the status the exec
    # really ended with, never an invented killed status.
    container = _container()
    container.block_reads = True
    container.kill_races_exit = True
    session = _session(container)

    handle = await session.exec_start(["true"], timeout_seconds=30)
    reader = asyncio.ensure_future(_drain(handle))
    await asyncio.sleep(0)
    await handle.kill()
    async with asyncio.timeout(1):
        items = await reader

    assert items == [SandboxStreamExit(exit_code=0)]


async def test_a_signalled_exec_ends_with_the_status_its_kill_reported_though_the_engine_records_none() -> None:
    # The kill reports the status the command ended with: the output ends with it, whatever the
    # engine records for the exec, and the engine is never asked.
    container = _container()
    container.block_reads = True
    container.killed_exit_unrecorded = True
    session = _session(container)

    handle = await session.exec_start(["sleep", "9"], timeout_seconds=30)
    target = container.execs[0]
    inspected = target.inspect_calls
    reader = asyncio.ensure_future(_drain(handle))
    await asyncio.sleep(0)
    await handle.kill()
    async with asyncio.timeout(1):
        items = await reader

    assert items == [SandboxStreamExit(exit_code=137)]
    assert target.inspect_calls == inspected


def _cut_frame_after_a_whole_one(conn: PeerConnection) -> None:
    """An engine whose command printed one whole frame and the start of the next when the kill comes."""
    conn.answer()
    whole = bytes([1, 0, 0, 0]) + (6).to_bytes(4, "big") + b"ready\n"
    cut = bytes([1, 0, 0, 0]) + (100).to_bytes(4, "big") + b"star"
    conn.sock.sendall(whole + cut)
    conn.wait_for_client_close()


async def test_a_kill_while_an_output_frame_is_arriving_ends_the_output_with_the_killed_status() -> None:
    peer = EnginePeer(_cut_frame_after_a_whole_one)
    container = _container()
    session = _peer_session(container, peer)

    handle = await session.exec_start(["yes"], timeout_seconds=5)
    output = handle.output
    async with asyncio.timeout(5):
        first = await anext(output)
        while len(handle._attachment._buffer) < 12:  # the cut frame's header and its first bytes are in
            await asyncio.sleep(0.01)
        await handle.kill()
        rest = [item async for item in output]

    assert first == SandboxStreamChunk(stream="stdout", data=b"ready\n")
    assert rest == [SandboxStreamExit(exit_code=137)]
    await asyncio.to_thread(peer.join)


async def test_a_callers_kill_joining_the_deadlines_kill_ends_with_the_exit_not_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _container()
    container.block_reads = True  # the attach stream never ends on its own
    session = _session(container)
    kills = 0
    deadline_kill_started = asyncio.Event()
    release = asyncio.Event()

    async def _held_kill_exec_group(
        target: FakeContainer, exec_obj: FakeExec, exec_id: str, retype: Any, attach: Any
    ) -> KillResult:
        nonlocal kills
        kills += 1
        # The deadline's kill: held in flight until the caller's kill has joined it.
        deadline_kill_started.set()
        await release.wait()
        return await kill_exec_group(target, exec_obj, exec_id, retype, attach)

    monkeypatch.setattr(sessions, "kill_exec_group", _held_kill_exec_group)
    handle = await session.exec_start(["cat"], timeout_seconds=0.1)
    reader = asyncio.ensure_future(_drain(handle))
    async with asyncio.timeout(1):
        await deadline_kill_started.wait()
        callers_kill = asyncio.ensure_future(handle.kill())
        await asyncio.sleep(0.01)
        assert not callers_kill.done()
        release.set()
        await callers_kill
        items = await reader

    assert items == [SandboxStreamExit(exit_code=137)]
    assert kills == 1


async def test_overlapping_kills_of_one_exec_run_one_sibling_kill_and_share_its_outcome() -> None:
    container = _container()
    container.block_reads = True
    container.kill_round_trip = 0.05  # both kills are in flight together
    session = _session(container)
    handle = await session.exec_start(["sleep", "9"], timeout_seconds=5)
    async with asyncio.timeout(1):
        await asyncio.gather(handle.kill(), handle.kill())
        items = await _drain(handle)
    assert [is_kill_exec(exec_obj.cmd) for exec_obj in container.execs] == [False, True]
    assert items == [SandboxStreamExit(exit_code=137)]


async def test_a_failed_shared_kill_raises_for_every_stop_that_awaited_it() -> None:
    container = _container()
    container.block_reads = True
    container.kill_round_trip = 0.05
    container.kill_failure = b"kill: permission denied"
    session = _session(container)
    handle = await session.exec_start(["sleep", "9"], timeout_seconds=5)
    results = await asyncio.gather(handle.kill(), handle.kill(), return_exceptions=True)
    assert [str(result) for result in results] == ["sandbox exec kill failed: kill: permission denied"] * 2
    assert [is_kill_exec(exec_obj.cmd) for exec_obj in container.execs] == [False, True]


async def test_a_write_whose_connection_is_lost_raises_and_every_later_write_does_too() -> None:
    container = _container()
    container.block_reads = True
    session = _session(container)

    handle = await session.exec_start(["head", "-c", "1"], timeout_seconds=30)
    container.stdin_ends_stream = True  # the connection is lost under the write
    for _ in range(3):
        with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
            await handle.write_stdin(b"x" * 2_000_000)
    await handle.kill()


async def test_a_write_after_the_engine_released_the_attach_connection_raises() -> None:
    container = _container()
    container.block_reads = True
    session = _session(container)

    handle = await session.exec_start(["head", "-c", "1"], timeout_seconds=30)
    attachment = container.execs[0].attachment
    assert attachment is not None
    attachment.engine_ends()  # the command exited and the engine closed the attach connection
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
        await handle.write_stdin(b"y")
    assert attachment.written == []
    await handle.kill()


async def test_a_kill_after_a_failed_stdin_write_still_kills_the_process() -> None:
    container = _container()
    container.block_reads = True  # the command keeps running after its stdin breaks
    session = _session(container)

    handle = await session.exec_start(["cat"], timeout_seconds=30)
    container.stdin_ends_stream = True  # the connection is lost under the write
    with pytest.raises(SandboxError, match="stream is closed"):
        await handle.write_stdin(b"x")
    await handle.kill()

    assert container.execs[0].killed is True
    async with asyncio.timeout(1):
        assert await _drain(handle) == [SandboxStreamExit(exit_code=137)]


async def test_a_kill_that_fails_raises_and_ends_nothing() -> None:
    container = _container()
    container.kill_failure = b"sh: can't kill pid -7: Operation not permitted\n"
    session = _session(container)

    handle = await session.exec_start(["cat"], timeout_seconds=5)
    with pytest.raises(SandboxError, match=r"kill failed: .*Operation not permitted"):
        await handle.kill()

    # The exec is not marked killed: its output still ends with its own exit code, never 137.
    assert [item async for item in handle.output] == [
        SandboxStreamChunk(stream="stdout", data=b""),
        SandboxStreamExit(exit_code=0),
    ]


def _assert_the_held_output_was_noted(exc: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    assert exc.__notes__ == [OUTPUT_HELD_NOTE]
    [record] = [record for record in caplog.records if record.levelname == "WARNING"]
    assert OUTPUT_HELD_NOTE in record.getMessage()


async def test_a_one_shot_exec_past_its_timeout_counts_the_output_that_reached_it_by_its_kill() -> None:
    container = _container()
    container.block_reads = True
    container.received = [SandboxStreamChunk(stream="stdout", data=b"early")]
    container.output_at_kill = [
        SandboxStreamChunk(stream="stdout", data=b"x" * 300),
        SandboxStreamChunk(stream="stderr", data=b"e" * 7),
    ]
    session = _session(container)

    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(session.exec(["sleep", "9"], timeout_seconds=0.05), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (5 + 300, 7)
    assert getattr(raised.value, "__notes__", []) == []


async def test_a_one_shot_exec_past_its_timeout_whose_stream_stays_open_after_its_kill_notes_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions, "_STREAM_END_GRACE_SECONDS", 0.1)
    container = _container()
    container.block_reads = True
    container.killed_stream_stays_open = True
    container.output_at_kill = [SandboxStreamChunk(stream="stdout", data=b"x" * 300)]
    session = _session(container)

    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(session.exec(["sleep", "9"], timeout_seconds=0.05), 5)
    assert (raised.value.stdout_len, raised.value.stderr_len) == (300, 0)
    assert raised.value.__notes__ == [
        "the timeout's count may miss output: the read of the sandbox exec did not end within 0.1s of the kill"
    ]


@pytest.mark.parametrize("finds_nothing", [True, False])
async def test_a_one_shot_exec_past_its_timeout_whose_output_a_process_still_holds_notes_it(
    caplog: pytest.LogCaptureFixture, finds_nothing: bool
) -> None:
    container = _container()
    container.block_reads = True
    container.kill_finds_nothing = finds_nothing
    container.kill_leaves_output_held = True
    session = _session(container)

    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(session.exec(["sleep", "9"], timeout_seconds=0.05), 5)
    _assert_the_held_output_was_noted(raised.value, caplog)


@pytest.mark.parametrize("finds_nothing", [True, False])
async def test_an_interactive_exec_past_its_deadline_whose_output_a_process_still_holds_times_out_at_once(
    caplog: pytest.LogCaptureFixture, finds_nothing: bool
) -> None:
    container = _container()
    container.block_reads = True
    container.kill_finds_nothing = finds_nothing
    container.kill_leaves_output_held = True
    session = _session(container)

    handle = await session.exec_start(["sleep", "9"], timeout_seconds=0.1)
    with pytest.raises(SandboxExecTimeoutError) as raised:
        await asyncio.wait_for(_drain(handle), 1)
    _assert_the_held_output_was_noted(raised.value, caplog)
    stream = container.execs[0].attachment
    assert stream is not None
    assert stream.closed.is_set()


# How long a test waits for an output that must end within the status grace it patches down.
_FAR_BELOW_THE_GRACE = 1.0


async def test_a_kill_that_finds_nothing_running_takes_the_end_that_comes_within_the_grace() -> None:
    container = _container()
    container.exec_frames = [(1, b"tail\n")]
    container.exec_exit_code = 4
    container.kill_finds_nothing = True
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=5)
    await handle.kill()
    async with asyncio.timeout(_FAR_BELOW_THE_GRACE):
        items = await _drain(handle)

    assert items == [SandboxStreamChunk(stream="stdout", data=b"tail\n"), SandboxStreamExit(exit_code=4)]


async def test_a_kill_that_finds_nothing_running_raises_when_the_engine_keeps_the_output_open_past_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions.DockerSandboxExecHandle, "_status_grace_seconds", 0.1)
    container = _container()
    container.block_reads = True  # the engine never ends the stream of the exited command
    container.kill_finds_nothing = True
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=60)
    await handle.kill()
    with pytest.raises(
        SandboxError,
        match=(
            r"^sandbox exec stream did not end within 0\.1s of the exec's end: the engine did not close the "
            r"stream of an exec that has exited with nothing holding its output$"
        ),
    ):
        async with asyncio.timeout(_FAR_BELOW_THE_GRACE):
            await _drain(handle)
    stream = container.execs[0].attachment
    assert stream is not None
    assert stream.closed.is_set()


@pytest.mark.parametrize("races_exit", [False, True], ids=["command-signalled", "command-exited"])
async def test_a_signalled_kill_that_leaves_the_output_held_ends_the_output_at_once_with_the_commands_status(
    races_exit: bool,
) -> None:
    # A process the command started still holds the output once the kill is done, so the engine
    # records no exit code for the exec: the output ends at once with the status the kill
    # reported, the killed status or the command's own.
    container = _container()
    container.block_reads = True
    container.kill_leaves_output_held = True
    container.kill_races_exit = races_exit
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=60)
    target = container.execs[0]
    inspected = target.inspect_calls
    await handle.kill()
    async with asyncio.timeout(_FAR_BELOW_THE_GRACE):
        items = await _drain(handle)
    assert items == [SandboxStreamExit(exit_code=0 if races_exit else 137)]
    assert target.inspect_calls == inspected


@pytest.mark.parametrize("output_held", [False, True], ids=["nothing-holds", "output-held"])
async def test_a_signalled_kill_yields_the_output_received_before_it_then_the_killed_status(output_held: bool) -> None:
    container = _container()
    container.block_reads = True
    container.kill_leaves_output_held = output_held
    container.received = [
        SandboxStreamChunk(stream="stdout", data=b"before"),
        SandboxStreamChunk(stream="stderr", data=b"warn"),
    ]
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=60)
    await handle.kill()
    async with asyncio.timeout(_FAR_BELOW_THE_GRACE):
        items = await _drain(handle)
    assert items == [
        SandboxStreamChunk(stream="stdout", data=b"before"),
        SandboxStreamChunk(stream="stderr", data=b"warn"),
        SandboxStreamExit(exit_code=137),
    ]


async def test_a_kill_finding_nothing_running_while_the_output_is_held_ends_the_output_with_a_sandbox_error() -> None:
    # Nothing of the exec was left to kill while a process the command started still holds its
    # output: the kill read no status, the engine records none while the output is held, and
    # none is read or invented.
    container = _container()
    container.block_reads = True
    container.kill_finds_nothing = True
    container.kill_leaves_output_held = True
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=60)
    target = container.execs[0]
    inspected = target.inspect_calls
    await handle.kill()
    with pytest.raises(
        SandboxError,
        match=(
            r"^sandbox exec output ended by the kill while a process the command started still holds it, "
            r"and the exec's exit status cannot be known: the kill found nothing of the exec left to kill, "
            r"so it read no status, and the engine records no exit code for the exec until nothing holds "
            r"its output$"
        ),
    ):
        async with asyncio.timeout(_FAR_BELOW_THE_GRACE):
            await _drain(handle)
    assert target.inspect_calls == inspected


async def test_a_kill_that_finds_nothing_running_ends_a_reader_already_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions.DockerSandboxExecHandle, "_status_grace_seconds", 0.1)
    container = _container()
    container.block_reads = True
    container.kill_finds_nothing = True
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=60)
    reader = asyncio.ensure_future(_drain(handle))
    await asyncio.sleep(0.05)  # the reader is waiting for the output
    assert not reader.done()
    await handle.kill()
    # The kill wakes the waiting reader, so the grace bounds its end, never the 60s deadline.
    with pytest.raises(SandboxError, match="of the exec's end: the engine did not close the stream"):
        async with asyncio.timeout(_FAR_BELOW_THE_GRACE):
            await reader


async def test_a_kill_that_finds_nothing_running_arms_the_grace_at_the_first_read_not_at_the_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions.DockerSandboxExecHandle, "_status_grace_seconds", 0.1)
    container = _container()
    container.exec_frames = [(1, b"tail\n")]
    container.exec_exit_code = 6
    container.kill_finds_nothing = True
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=5)
    await handle.kill()
    await asyncio.sleep(0.2)  # past the grace before any read: the buffered output still counts
    async with asyncio.timeout(_FAR_BELOW_THE_GRACE):
        items = await _drain(handle)

    assert items == [SandboxStreamChunk(stream="stdout", data=b"tail\n"), SandboxStreamExit(exit_code=6)]


@pytest.mark.parametrize("deadline_kill", [True, False])
async def test_an_exec_ended_in_time_gives_all_its_output_to_a_reader_slower_than_the_grace(
    monkeypatch: pytest.MonkeyPatch, deadline_kill: bool
) -> None:
    monkeypatch.setattr(sessions.DockerSandboxExecHandle, "_status_grace_seconds", 0.1)
    container = _container()
    frames = [(1, b"a"), (2, b"b"), (1, b"c"), (1, b"d")]
    container.exec_frames = list(frames)
    container.exec_exit_code = 0
    container.kill_finds_nothing = True
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=0.05 if deadline_kill else 60)
    # The engine shows the exec ended; its output waits for the reader.
    container.execs[0].running = False
    if deadline_kill:
        await asyncio.sleep(0.1)
    else:
        await handle.kill()
    items: list[SandboxStreamChunk | SandboxStreamExit] = []
    async with asyncio.timeout(5):
        async for item in handle.output:
            items.append(item)
            await asyncio.sleep(0.15)  # longer than the grace, after every chunk

    assert items == [
        *(SandboxStreamChunk(stream="stderr" if stream_id == 2 else "stdout", data=data) for stream_id, data in frames),
        SandboxStreamExit(exit_code=0),
    ]


async def test_a_kill_that_finds_nothing_running_leaves_stdin_open_to_writes_and_its_end() -> None:
    container = _container()
    container.block_reads = True  # the command removed its pidfile and runs on
    container.kill_finds_nothing = True
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=60)
    await handle.kill()
    await handle.write_stdin(b"still read\n")
    await handle.close_stdin()

    attachment = container.execs[0].attachment
    assert attachment is not None
    assert attachment.written == [b"still read\n"]
    assert attachment.input_ended


async def test_a_write_past_the_grace_of_a_kill_that_found_nothing_running_reports_the_closed_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions.DockerSandboxExecHandle, "_status_grace_seconds", 0.1)
    container = _container()
    container.block_reads = True
    container.kill_finds_nothing = True
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=60)
    await handle.kill()
    with pytest.raises(SandboxError, match="of the exec's end: the engine did not close the stream"):
        async with asyncio.timeout(_FAR_BELOW_THE_GRACE):
            await _drain(handle)
    # The command may still be running: only the stream the handle closed is gone.
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
        await handle.write_stdin(b"late\n")


async def test_an_exec_start_cancelled_while_it_attaches_kills_the_started_exec() -> None:
    container = _container()
    session = _session(container)
    attached = asyncio.Event()

    async def _attach_then_hang(attachment: FakeAttachment) -> None:
        # The engine has started the exec; its answer to the attach never comes back.
        attached.set()
        await asyncio.Event().wait()

    container.attach_hook = _attach_then_hang
    call = asyncio.ensure_future(session.exec_start(["sleep", "9"], timeout_seconds=30))
    await attached.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert target.killed is True
    assert target.attachment is not None
    assert target.attachment.closed.is_set()


async def test_an_exec_start_whose_attach_fails_kills_the_exec() -> None:
    container = _container()
    session = _session(container)

    async def _attach_fails(attachment: FakeAttachment) -> None:
        # The engine has started the exec, then the attach fails.
        raise DockerError(500, "attach failed")

    container.attach_hook = _attach_fails
    with pytest.raises(SandboxError, match=r"\[500\]: attach failed"):
        await session.exec_start(["sleep", "9"], timeout_seconds=30)

    target, killer = container.execs
    assert is_kill_exec(killer.cmd)
    assert target.killed is True
    assert target.attachment is not None
    assert target.attachment.closed.is_set()


async def test_exec_start_starts_the_exec() -> None:
    container = _container()
    session = _session(container)

    await session.exec_start(["cat"], timeout_seconds=5)

    assert container.execs[0].started is True
    assert container.execs[0].attachment is not None


async def _drain(handle) -> list[SandboxStreamChunk | SandboxStreamExit]:
    return [item async for item in handle.output]


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

    assert any(is_kill_exec(e.cmd) for e in container.execs)


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

    container = _container()

    async def _boom(path: str, data: bytes) -> None:
        raise DockerError(500, "archive rejected")

    container.put_archive = _boom  # type: ignore[method-assign]
    session = _session(container)
    with pytest.raises(SandboxError, match="docker engine error"):
        await session.put_file("note.txt", b"bytes")


def test_merge_env_unwraps_and_overlays() -> None:
    base = {"A": SecretStr("1"), "B": SecretStr("2")}
    overlay = {"B": SecretStr("9"), "C": SecretStr("3")}
    assert merge_env(base, overlay) == {"A": "1", "B": "9", "C": "3"}
    assert merge_env(base, None) == {"A": "1", "B": "2"}


def test_engine_error_carries_status_and_message() -> None:

    wrapped = engine_error(DockerError(500, "boom"))
    assert isinstance(wrapped, SandboxError)
    assert "[500]" in str(wrapped)
    assert "boom" in str(wrapped)


async def test_kill_exec_group_reports_whether_it_signalled() -> None:
    container = _container()
    session = _session(container)
    handle = await session.exec_start(["cat"], timeout_seconds=5)
    exec_id = wrapped_exec_id(container.execs[0].cmd)
    assert exec_id is not None
    assert handle is not None

    target = container.execs[0]
    assert await kill_exec_group(container, target, exec_id, engine_error, container.attach_exec) == KillResult(
        KillOutcome.SIGNALLED, exit_code=137
    )
    assert target.killed is True
    # The exec has ended: the kill finds nothing running, and reports it with no inspect.
    inspects = target.inspect_calls
    assert await kill_exec_group(container, target, exec_id, engine_error, container.attach_exec) == KillResult(
        KillOutcome.NOTHING_RUNNING
    )
    assert target.inspect_calls == inspects
    # Nothing to kill while the exec still shows as running, as when its command removed its
    # own pidfile.
    target.running = True
    container.kill_finds_nothing = True
    assert await kill_exec_group(container, target, exec_id, engine_error, container.attach_exec) == KillResult(
        KillOutcome.NOTHING_RUNNING
    )
    # A process the command started still holds the exec's output: the kill reports it, with
    # no inspect in place of the fact.
    container.kill_leaves_output_held = True
    assert await kill_exec_group(container, target, exec_id, engine_error, container.attach_exec) == KillResult(
        KillOutcome.NOTHING_RUNNING, output_held=True
    )
    container.kill_finds_nothing = False
    assert await kill_exec_group(container, target, exec_id, engine_error, container.attach_exec) == KillResult(
        KillOutcome.SIGNALLED, exit_code=137, output_held=True
    )
    container.kill_leaves_output_held = False
    # The container is gone, and the exec with it.
    container.deleted = True
    assert await kill_exec_group(container, target, exec_id, engine_error, container.attach_exec) == KillResult(
        KillOutcome.SESSION_GONE
    )


async def test_kill_exec_group_raises_a_failed_kill() -> None:
    container = _container()
    container.kill_failure = b"sh: can't open /proc\n"
    target = await container.exec(cmd=["sh", "-c", EXEC_WRAPPER, "x1", "cat"])
    target.started = True  # the engine runs it, as once its attach is made
    with pytest.raises(SandboxError, match=r"^sandbox exec kill failed: sh: can't open /proc$"):
        await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)


async def test_a_kill_whose_follow_up_inspect_hangs_keeps_its_failure_on_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kill reports its failure, then the engine never answers the exec's inspect.
    monkeypatch.setattr(exec_kill, "KILL_TIMEOUT_SECONDS", 0.05)
    container = _container()
    container.kill_failure = b"sh: can't kill pid -7: Operation not permitted\n"
    target = await _running_target(container)
    target.inspect_hangs = True
    with pytest.raises(SandboxError, match=r"^sandbox exec kill did not finish within 0.05s") as raised:
        await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)
    assert raised.value.__notes__ == [
        "the sandbox exec kill failed before its time limit passed: "
        "sandbox exec kill failed: sh: can't kill pid -7: Operation not permitted"
    ]


async def test_a_kill_whose_stream_stops_mid_line_keeps_the_partial_line_on_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kill wrote part of its failure line, and the engine holds the rest of its stream.
    monkeypatch.setattr(exec_kill, "KILL_TIMEOUT_SECONDS", 0.05)
    container = _container()
    container.kill_stream_held = True
    container.kill_failure = b"sh: can't kill pid -7: Opera"
    target = await _running_target(container)
    with pytest.raises(SandboxError, match=r"^sandbox exec kill did not finish within 0.05s") as raised:
        await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)
    assert raised.value.__notes__ == [
        "the sandbox exec kill wrote on its stderr before its time limit passed: sh: can't kill pid -7: Opera"
    ]


async def test_a_kill_the_engine_refuses_keeps_its_refusal_when_the_inspect_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exec_kill, "KILL_TIMEOUT_SECONDS", 0.05)
    container = _container()
    target = await _running_target(container)
    target.inspect_hangs = True
    container.exec_error = 409
    with pytest.raises(SandboxError, match=r"^sandbox exec kill did not finish within 0.05s") as raised:
        await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)
    [note] = raised.value.__notes__
    assert note.startswith("the sandbox exec kill failed before its time limit passed: docker engine error [409]")


async def test_a_one_shot_timeout_whose_kill_times_out_after_failing_keeps_the_kills_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exec_kill, "KILL_TIMEOUT_SECONDS", 0.05)
    container = _container()
    container.block_reads = True
    container.kill_failure = b"sh: can't kill pid -7: Operation not permitted\n"
    session = _session(container)
    call = asyncio.ensure_future(session.exec(["sleep", "9"], timeout_seconds=0.05))
    while not container.execs or not container.execs[0].started:
        await asyncio.sleep(0)
    container.execs[0].inspect_hangs = True
    async with asyncio.timeout(2):
        with pytest.raises(SandboxExecTimeoutError) as raised:
            await call
    assert raised.value.__notes__[:2] == [
        "the kill of the sandbox exec also failed: sandbox exec kill did not finish within 0.05s",
        "the sandbox exec kill failed before its time limit passed: "
        "sandbox exec kill failed: sh: can't kill pid -7: Operation not permitted",
    ]


# How long a kill whose report is in hand may take, far below the kill's own time limit.
_FAR_BELOW_THE_KILL_TIMEOUT = 1.0


async def _running_target(container: FakeContainer) -> FakeExec:
    """A wrapped exec ``x1`` the engine runs, as once its attach is made."""
    target = await container.exec(cmd=["sh", "-c", EXEC_WRAPPER, "x1", "cat"])
    target.started = True
    return target


@pytest.mark.parametrize(
    ("finds_nothing", "held", "result"),
    [
        (False, False, KillResult(KillOutcome.SIGNALLED, exit_code=137)),
        (False, True, KillResult(KillOutcome.SIGNALLED, exit_code=137, output_held=True)),
        (True, False, KillResult(KillOutcome.NOTHING_RUNNING)),
        (True, True, KillResult(KillOutcome.NOTHING_RUNNING, output_held=True)),
    ],
)
async def test_a_kill_whose_end_the_engine_holds_returns_its_finding_at_once(
    finds_nothing: bool, held: bool, result: KillResult
) -> None:
    # The engine holds the kill's stream end and its exit code, as while another reader of the
    # container's output has paused: the finding on the kill's stdout is its report.
    container = _container()
    container.kill_stream_held = True
    container.kill_finds_nothing = finds_nothing
    container.kill_leaves_output_held = held
    target = await _running_target(container)
    async with asyncio.timeout(_FAR_BELOW_THE_KILL_TIMEOUT):
        assert await kill_exec_group(container, target, "x1", engine_error, container.attach_exec) == result
    (killer,) = [exec_obj for exec_obj in container.execs if is_kill_exec(exec_obj.cmd)]
    assert killer.running  # the kill's exit was never read


async def test_a_kill_that_finds_nothing_running_returns_it_without_the_exec_inspect() -> None:
    container = _container()
    container.kill_finds_nothing = True
    target = await _running_target(container)
    target.inspect_hangs = True  # the engine holds the exec's inspect
    async with asyncio.timeout(_FAR_BELOW_THE_KILL_TIMEOUT):
        assert await kill_exec_group(container, target, "x1", engine_error, container.attach_exec) == KillResult(
            KillOutcome.NOTHING_RUNNING
        )
    assert target.inspect_calls == 0


async def test_a_kill_whose_failure_line_arrives_on_a_stream_the_engine_holds_raises_it_at_once() -> None:
    container = _container()
    container.kill_stream_held = True
    container.kill_failure = b"cannot read the open files of process 19: ls: permission denied\n"
    target = await _running_target(container)
    async with asyncio.timeout(_FAR_BELOW_THE_KILL_TIMEOUT):
        with pytest.raises(
            SandboxError,
            match=r"^sandbox exec kill failed: cannot read the open files of process 19: ls: permission denied$",
        ):
            await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)


async def test_a_kill_whose_stdout_names_no_finding_raises_its_output() -> None:
    container = _container()
    container.kill_output = [(1, b"stopped\n")]
    target = await _running_target(container)
    with pytest.raises(SandboxError, match=r"^sandbox exec kill failed: stopped$"):
        await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)


async def test_a_signalled_kill_reporting_an_unreadable_status_raises() -> None:
    container = _container()
    container.kill_output = [(1, b"x\nsig"), (1, b"nalled\n")]
    target = await _running_target(container)
    with pytest.raises(
        SandboxError, match=r"^sandbox exec kill reported an unreadable exit status: b'x\\nsignalled\\n'$"
    ):
        await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)


async def test_a_kill_whose_stream_ends_with_no_whole_line_raises_its_output() -> None:
    container = _container()
    container.kill_output = [(1, b"signal"), (2, b"Killed")]
    target = await _running_target(container)
    with pytest.raises(SandboxError, match=r"^sandbox exec kill failed: signalKilled$"):
        await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)


async def test_a_deadline_kill_whose_end_the_engine_holds_times_out_at_once_counting_the_output_received(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing has read the output since the start; the attachment holds 128 KiB of it. The kill
    # finds a process the command started still holding the output, and the engine holds the
    # kill's end behind the paused stream: the timeout comes at once, counting what was received.
    container = _container()
    container.block_reads = True
    container.kill_stream_held = True
    container.kill_finds_nothing = True
    container.kill_leaves_output_held = True
    container.received = [SandboxStreamChunk(stream="stdout", data=b"y" * 4096) for _ in range(32)]
    session = _session(container)

    handle = await session.exec_start(["sh"], timeout_seconds=0.05)
    await asyncio.sleep(0.3)  # past the deadline, nothing reading
    async with asyncio.timeout(_FAR_BELOW_THE_KILL_TIMEOUT):
        with pytest.raises(SandboxExecTimeoutError) as raised:
            await _drain(handle)

    assert (raised.value.stdout_len, raised.value.stderr_len) == (128 * 1024, 0)
    _assert_the_held_output_was_noted(raised.value, caplog)
    stream = container.execs[0].attachment
    assert stream is not None
    assert stream.closed.is_set()


async def test_kill_exec_group_raises_an_engine_error() -> None:
    container = _container()
    target = await container.exec(cmd=["sh", "-c", EXEC_WRAPPER, "x1", "cat"])
    target.started = True  # the engine runs it, as once its attach is made
    container.exec_raises = True  # the sibling kill exec cannot be created
    with pytest.raises(SandboxError, match="docker engine error"):
        await kill_exec_group(container, target, "x1", engine_error, container.attach_exec)


async def test_a_kill_reads_its_output_over_the_providers_exec_attachment_and_closes_it() -> None:
    container = _container()
    container.block_reads = True
    attached: list[str] = []
    kill_streams: list[Any] = []

    async def attach(exec_id: str) -> ExecAttachment:
        attached.append(exec_id)
        attachment = await container.attach_exec(exec_id)
        if is_kill_exec(container.execs[-1].cmd):
            kill_streams.append(attachment)
        return attachment

    sandbox = DockerSandbox(docker=object(), attach=attach, settings=DockerSandboxSettings(host="tcp://engine:2376"))
    session = DockerSandboxSession(
        sandbox=sandbox, session_id="c1", container=container, workspace_key="ws1", durability="ephemeral", base_env={}
    )
    handle = await session.exec_start(["cat"], timeout_seconds=5)
    async with asyncio.timeout(1):
        await handle.kill()
        items = await _drain(handle)

    assert items == [SandboxStreamExit(exit_code=137)]
    assert attached == [exec_obj.id for exec_obj in container.execs]
    assert [is_kill_exec(exec_obj.cmd) for exec_obj in container.execs] == [False, True]
    assert [stream.closed for stream in kill_streams] == [True]


async def test_a_kill_whose_attach_the_engine_answers_with_a_missing_container_finds_the_session_gone() -> None:
    container = _container()
    target = await container.exec(cmd=["sh", "-c", EXEC_WRAPPER, "x1", "cat"])
    target.started = True  # the engine runs it, as once its attach is made

    async def attach(exec_id: str) -> ExecAttachment:
        error = DockerError(404, "no such container: c1")
        raise engine_error(error) from error

    assert await kill_exec_group(container, target, "x1", engine_error, attach) == KillResult(KillOutcome.SESSION_GONE)


async def test_a_kill_whose_attach_fails_raises_its_error() -> None:
    container = _container()
    target = await container.exec(cmd=["sh", "-c", EXEC_WRAPPER, "x1", "cat"])
    target.started = True  # the engine runs it, as once its attach is made

    async def attach(exec_id: str) -> ExecAttachment:
        raise SandboxError("cannot reach the Docker engine: connection refused")

    with pytest.raises(SandboxError, match="cannot reach the Docker engine"):
        await kill_exec_group(container, target, "x1", engine_error, attach)


async def test_read_exit_code_times_out() -> None:
    exec_obj = FakeExec(container=_container(), exec_id="e1", cmd=["x"], frames=[], exit_code=0)
    exec_obj.running = True  # never reports an exit
    with pytest.raises(SandboxError, match=r"reported no exit code for the exec within 0\.05s"):
        await read_exit_code(exec_obj, engine_error, within_seconds=0.05)


async def test_a_close_stdin_of_a_stream_the_engine_closed_raises_and_leaves_stdin_closed() -> None:
    container = _container()
    container.block_reads = True
    session = _session(container)

    handle = await session.exec_start(["cat"], timeout_seconds=30)
    attachment = container.execs[0].attachment
    assert attachment is not None
    attachment.engine_ends()  # the engine dropped the attach connection
    with pytest.raises(SandboxError, match=r"^cannot close stdin: the sandbox exec stream is closed$"):
        await handle.close_stdin()
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
        await handle.write_stdin(b"x")
    await handle.kill()


async def test_an_interactive_exec_whose_attach_drops_is_still_stopped_by_a_kill() -> None:
    container = _container()
    container.attach_drops = True
    session = _session(container)

    handle = await session.exec_start(["sleep", "100"], timeout_seconds=30)
    with pytest.raises(SandboxError, match=r"^sandbox exec stream ended, but the engine reported no exit code"):
        await _drain(handle)
    with pytest.raises(SandboxError, match=r"^cannot write stdin: the sandbox exec stream is closed$"):
        await handle.write_stdin(b"x")
    await handle.kill()

    (command,) = [exec_obj for exec_obj in container.execs if not is_kill_exec(exec_obj.cmd)]
    assert command.killed
