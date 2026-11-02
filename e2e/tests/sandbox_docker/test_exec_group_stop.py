"""A stop of an exec whose command exited, leaving a process in its group, on the REAL Docker provider.

The command exits at once, leaving a process in its own process group that holds the exec's
output and records its pid. The exec runs on while that process holds its output, and a stop
past the command's exit kills it with the rest of the group: the deadline of a one-shot exec
and of an interactive one times out with no holder noted, a ``kill()`` ends the output with
the command's own exit status, and the process is gone after each. Several execs that each
left such a process leave no zombie behind: the session container's init reaps what the
wrapper keeps in each command's group.

Docker-gated: the parent conftest keeps this module out of collection unless
``TAI_E2E_SANDBOX_DOCKER=1``, and it skips LOUDLY when ``SANDBOX_DOCKER_TEST_HOST``
names no engine.
"""

from __future__ import annotations

import time

import pytest
from tai42_contract.sandbox import (
    SandboxExecTimeoutError,
    SandboxSessionSpec,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_kit.sandbox.exec_kill import KILL_TIMEOUT_SECONDS
from tai42_kit.sandbox.session import ManagedSandboxSession

from tai42_e2e.waiting import wait_for_async

from ._support import TEST_IMAGE, open_sandbox, requires_engine

# The file the process the command leaves in its group records its pid in.
_LEFT_PID = "/tmp/left-in-group.pid"

# A command that exits at once, leaving a process in its group that records its pid, writes it
# on the exec's stdout and sleeps for a minute.
_LEAVES_A_PROCESS_IN_ITS_GROUP = [
    "sh",
    "-c",
    f"sh -c 'echo $$ > {_LEFT_PID}; echo $$; exec sleep 60' & exit 0",
]

# Prints ``ended`` once the process whose pid ``$1`` names has exited (no ``/proc/<pid>/stat``,
# or state ``Z``), else ``running``.
_PROCESS_STATE = (
    's="$(cat /proc/"$1"/stat 2>/dev/null)"; r="${s##*) }"; '
    '{ [ -z "$s" ] || [ "${r%% *}" = Z ]; } && echo ended || echo running'
)

# Prints ``exited`` once the leader of the process group of the process whose pid ``$1`` names,
# the exec's command itself, has exited and been reaped (no ``/proc/<pgid>/stat``).
_COMMAND_EXITED = 's="$(cat /proc/"$1"/stat)" || exit 1; set -- ${s##*) }; [ -e /proc/"$3"/stat ] || echo exited'

# Prints one line per process in state ``Z`` (``/proc/<pid>/stat``, read past the last ``) ``
# since the command name may hold any character), listing again every 0.1 s for up to 2 s while
# one is listed: the init reaps a process the wrapper kept only once the wrapper has ended.
_LIST_ZOMBIES = (
    'n=0; while z="$(for f in /proc/[0-9]*/stat; do read -r l < "$f" 2>/dev/null || continue; '
    'r="${l##*) }"; [ "${r%% *}" = Z ] && echo "$l"; done)"; [ -n "$z" ] && [ "$n" -lt 20 ]; do '
    'n=$((n + 1)); sleep 0.1; done; [ -n "$z" ] && echo "$z"; exit 0'
)


def _spec(workspace_key: str) -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key=workspace_key,
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
    )


async def _left_process_state(session: ManagedSandboxSession) -> str:
    """``ended`` or ``running``: the state of the process the command left, by the pid it recorded."""
    left = await session.exec(["cat", _LEFT_PID], timeout_seconds=30)
    assert left.exit_code == 0, left.stderr
    state = await session.exec(["sh", "-c", _PROCESS_STATE, "sh", left.stdout.strip()], timeout_seconds=30)
    return state.stdout.strip()


async def _until_the_command_exited(session: ManagedSandboxSession, left_pid: str) -> None:
    """Return once the exec's command has exited, leaving the process whose pid ``left_pid`` names in its group."""

    async def exited() -> bool:
        probe = await session.exec(["sh", "-c", _COMMAND_EXITED, "sh", left_pid], timeout_seconds=30)
        assert probe.exit_code == 0, probe.stderr
        return probe.stdout.strip() == "exited"

    await wait_for_async(exited, deadline=10, message="the exec's command never exited")


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_a_one_shot_deadline_kills_the_process_the_command_left_in_its_group() -> None:
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(_spec("exec-group-stop-one-shot"))
        try:
            started = time.monotonic()
            with pytest.raises(SandboxExecTimeoutError) as raised:
                await session.exec(_LEAVES_A_PROCESS_IN_ITS_GROUP, timeout_seconds=3)
            took = time.monotonic() - started
            assert getattr(raised.value, "__notes__", []) == []
            assert raised.value.stdout_len > 0
            assert 3 <= took < 3 + KILL_TIMEOUT_SECONDS
            assert await _left_process_state(session) == "ended"
        finally:
            await session.destroy()


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_an_interactive_deadline_kills_the_process_the_command_left_in_its_group() -> None:
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(_spec("exec-group-stop-interactive"))
        try:
            handle = await session.exec_start(_LEAVES_A_PROCESS_IN_ITS_GROUP, timeout_seconds=3)
            started = time.monotonic()
            with pytest.raises(SandboxExecTimeoutError) as raised:
                async for _ in handle.output:
                    pass
            took = time.monotonic() - started
            assert getattr(raised.value, "__notes__", []) == []
            assert took < 3 + KILL_TIMEOUT_SECONDS
            assert await _left_process_state(session) == "ended"
        finally:
            await session.destroy()


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_a_kill_after_the_command_exited_kills_the_process_it_left_in_its_group() -> None:
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(_spec("exec-group-stop-kill"))
        try:
            handle = await session.exec_start(_LEAVES_A_PROCESS_IN_ITS_GROUP, timeout_seconds=60)
            output = handle.output
            first = await anext(output)
            assert isinstance(first, SandboxStreamChunk)
            assert first.stream == "stdout"
            await _until_the_command_exited(session, first.data.decode().strip())
            started = time.monotonic()
            await handle.kill()
            rest = [item async for item in output]
            assert time.monotonic() - started < KILL_TIMEOUT_SECONDS / 2
            assert rest == [SandboxStreamExit(exit_code=0)]
            assert await _left_process_state(session) == "ended"
        finally:
            await session.destroy()


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_execs_that_left_a_process_in_their_group_leave_no_zombie_behind() -> None:
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(_spec("exec-group-stop-reap"))
        try:
            for _ in range(3):
                handle = await session.exec_start(_LEAVES_A_PROCESS_IN_ITS_GROUP, timeout_seconds=60)
                output = handle.output
                first = await anext(output)
                assert isinstance(first, SandboxStreamChunk)
                await _until_the_command_exited(session, first.data.decode().strip())
                await handle.kill()
                async for _ in output:
                    pass
            zombies = await session.exec(["sh", "-c", _LIST_ZOMBIES], timeout_seconds=30)
            assert zombies.exit_code == 0
            assert zombies.stdout == ""
        finally:
            await session.destroy()
