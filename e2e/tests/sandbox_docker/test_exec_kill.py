"""Killed execs on the REAL Docker provider over a live engine: the wrapper reaps its command.

A killed command is reaped by the exec wrapper that started it, and every process the exec
leaves behind by the engine's init the session container runs as its main process. One case
kills running commands and then lists the container's zombie processes: none may remain, and
each killed exec's output after the line its command printed is the killed status alone, no
stderr from the wrapper. Another kills a command whose own child is still in its process group:
the kill stops that child too. The last kills a command that writes without pause, so the kill
lands with part of an output frame received: its output still ends with the killed status,
never an error.

Docker-gated: the parent conftest keeps this module out of collection unless
``TAI_E2E_SANDBOX_DOCKER=1``, and it skips LOUDLY when ``SANDBOX_DOCKER_TEST_HOST``
names no engine.
"""

from __future__ import annotations

import signal

import pytest
from tai42_contract.sandbox import SandboxSessionSpec, SandboxStreamChunk, SandboxStreamExit

from ._support import TEST_IMAGE, open_sandbox, requires_engine

_KILLED_EXIT_CODE = 128 + signal.SIGKILL

# Prints one line per process in state ``Z`` (``/proc/<pid>/stat``, read past the last ``) ``
# since the command name may hold any character).
_LIST_ZOMBIES = (
    'for f in /proc/[0-9]*/stat; do read -r l < "$f" 2>/dev/null || continue; '
    'r="${l##*) }"; [ "${r%% *}" = Z ] && echo "$l"; done; exit 0'
)


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_killed_execs_leave_no_zombie_and_end_with_the_killed_status() -> None:
    spec = SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key="exec-kill-reap",
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
    )
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(spec)
        try:
            for _ in range(3):
                handle = await session.exec_start(["sh", "-c", "echo ready; exec sleep 100"], timeout_seconds=30)
                output = handle.output
                first = await anext(output)
                assert first == SandboxStreamChunk(stream="stdout", data=b"ready\n")
                await handle.kill()
                rest = [item async for item in output]
                assert rest == [SandboxStreamExit(exit_code=_KILLED_EXIT_CODE)]
            zombies = await session.exec(["sh", "-c", _LIST_ZOMBIES], timeout_seconds=30)
            assert zombies.exit_code == 0
            assert zombies.stdout == ""
        finally:
            await session.destroy()


# Prints ``ended`` once the process whose pid ``$1`` names has exited (no ``/proc/<pid>/stat``,
# or state ``Z``), else ``running``.
_PROCESS_STATE = (
    's="$(cat /proc/"$1"/stat 2>/dev/null)"; r="${s##*) }"; '
    '{ [ -z "$s" ] || [ "${r%% *}" = Z ]; } && echo ended || echo running'
)


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_a_kill_stops_the_child_a_command_started_in_its_process_group() -> None:
    spec = SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key="exec-kill-group",
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
    )
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(spec)
        try:
            handle = await session.exec_start(["sh", "-c", 'sleep 100 & echo "$!"; wait'], timeout_seconds=30)
            output = handle.output
            first = await anext(output)
            assert isinstance(first, SandboxStreamChunk)
            assert first.stream == "stdout"
            child = first.data.decode().strip()
            await handle.kill()
            rest = [item async for item in output]
            assert rest == [SandboxStreamExit(exit_code=_KILLED_EXIT_CODE)]
            state = await session.exec(["sh", "-c", _PROCESS_STATE, "sh", child], timeout_seconds=30)
            assert state.stdout.strip() == "ended"
        finally:
            await session.destroy()


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_a_kill_of_a_command_writing_without_pause_ends_its_output_with_the_killed_status() -> None:
    spec = SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key="exec-kill-chatty",
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
    )
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(spec)
        try:
            # ``cat /dev/zero`` fills each output frame to the engine's copy buffer, larger than one
            # TLS record, so the kill lands with part of a frame received, each time at another point.
            for _ in range(5):
                handle = await session.exec_start(["cat", "/dev/zero"], timeout_seconds=30)
                output = handle.output
                first = await anext(output)
                assert isinstance(first, SandboxStreamChunk)
                await handle.kill()
                rest = [item async for item in output]
                assert rest[-1] == SandboxStreamExit(exit_code=_KILLED_EXIT_CODE)
                assert all(isinstance(item, SandboxStreamChunk) and item.stream == "stdout" for item in rest[:-1])
        finally:
            await session.destroy()
