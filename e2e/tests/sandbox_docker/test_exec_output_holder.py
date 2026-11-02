"""An exec whose command exits while a process it started holds its output, on the REAL Docker provider.

The exec runs while a process its command started holds its output: the call returns the
command's exit code and the whole output once that process has ended, however long after the
engine's own close of an exited exec's stream; at its deadline it times out with the holder
noted; and a ``kill()`` that leaves the output held ends the output at once with the
command's own exit code, which the kill read from the wrapper, though the engine records no exit
code for an exec whose output is still held.

Docker-gated: the parent conftest keeps this module out of collection unless
``TAI_E2E_SANDBOX_DOCKER=1``, and it skips LOUDLY when ``SANDBOX_DOCKER_TEST_HOST``
names no engine.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from tai42_contract.sandbox import (
    SandboxExecTimeoutError,
    SandboxSessionSpec,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_kit.sandbox.exec_kill import KILL_TIMEOUT_SECONDS, OUTPUT_HELD_NOTE

from ._support import TEST_IMAGE, open_sandbox, requires_engine

# A command that exits at once, leaving a process in its group that writes a line a second for
# five seconds on the exec's stdout.
_HELD_FOR_FIVE_SECONDS_ARGV = ["sh", "-c", "(for i in 1 2 3 4 5; do echo $i; sleep 1; done) & exit 0"]
# A command that exits at once, leaving a process outside its group (``setsid``) that writes a
# line a second for two minutes on the exec's stdout.
_HELD_FOR_TWO_MINUTES_ARGV = [
    "sh",
    "-c",
    'setsid sh -c \'i=1; while [ "$i" -le 120 ]; do echo "$i"; i=$((i + 1)); sleep 1; done\' & exit 0',
]


def _spec(workspace_key: str) -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key=workspace_key,
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
    )


async def _read_to_end(
    output: AsyncIterator[SandboxStreamChunk | SandboxStreamExit],
) -> list[SandboxStreamChunk | SandboxStreamExit]:
    return [item async for item in output]


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_an_exec_whose_output_a_process_it_started_holds_returns_that_output_to_its_end() -> None:
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(_spec("exec-output-holder-to-end"))
        try:
            started = time.monotonic()
            result = await session.exec(_HELD_FOR_FIVE_SECONDS_ARGV, timeout_seconds=10)
            took = time.monotonic() - started
            assert (result.exit_code, result.stdout, result.stderr) == (0, "1\n2\n3\n4\n5\n", "")
            assert took >= 4
        finally:
            await session.destroy()


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_an_exec_whose_output_is_held_past_its_deadline_times_out_with_the_holder_noted() -> None:
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(_spec("exec-output-holder-deadline"))
        try:
            started = time.monotonic()
            with pytest.raises(SandboxExecTimeoutError) as raised:
                await session.exec(_HELD_FOR_TWO_MINUTES_ARGV, timeout_seconds=4)
            took = time.monotonic() - started
            assert raised.value.__notes__ == [OUTPUT_HELD_NOTE]
            assert raised.value.stdout_len >= len("1\n2\n3\n4\n")
            assert 4 <= took < 4 + KILL_TIMEOUT_SECONDS
        finally:
            await session.destroy()


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_a_kill_that_leaves_the_output_held_ends_the_output_at_once_with_the_commands_status() -> None:
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(_spec("exec-output-holder-kill"))
        try:
            handle = await session.exec_start(_HELD_FOR_TWO_MINUTES_ARGV, timeout_seconds=30)
            output = handle.output
            assert await anext(output) == SandboxStreamChunk(stream="stdout", data=b"1\n")
            started = time.monotonic()
            await handle.kill()
            items = await asyncio.wait_for(_read_to_end(output), KILL_TIMEOUT_SECONDS)
            assert time.monotonic() - started < KILL_TIMEOUT_SECONDS / 2
            assert items[-1] == SandboxStreamExit(exit_code=0)
            assert all(isinstance(item, SandboxStreamChunk) for item in items[:-1])
        finally:
            await session.destroy()
