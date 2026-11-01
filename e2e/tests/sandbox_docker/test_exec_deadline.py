"""An exec's deadline on the REAL Docker provider over a live engine, its reader paused.

A command that writes without pause and never ends, read one chunk and then not until past
its deadline: the paused reader leaves the output the engine holds for it unread, and while
it does the engine holds the end of every other exec in the session, the deadline's kill
included. The kill reports what it found as a line of its own output, which the engine
still delivers, so the deadline's stop does not wait on the engine: once the reader reads
on, the exec times out at once, its kill noted as failed nowhere.

Docker-gated: the parent conftest keeps this module out of collection unless
``TAI_E2E_SANDBOX_DOCKER=1``, and it skips LOUDLY when ``SANDBOX_DOCKER_TEST_HOST``
names no engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from tai42_contract.sandbox import SandboxExecTimeoutError, SandboxSessionSpec, SandboxStreamChunk, SandboxStreamExit

from ._support import TEST_IMAGE, open_sandbox, requires_engine

# The command: it writes to its stdout without pause, more than every buffer between it and
# the reader holds, and never ends.
_ENDLESS_WRITER_ARGV = ["sh", "-c", "exec cat /dev/zero"]
_TIMEOUT_SECONDS = 1
# Past the deadline, and well short of the kill's own bound past it.
_READER_PAUSE_SECONDS = 3
# How soon the timeout comes once the reader reads on.
_TIMEOUT_RAISED_SECONDS = 2


async def _read_to_end(output: AsyncIterator[SandboxStreamChunk | SandboxStreamExit]) -> None:
    async for _ in output:
        pass


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_a_command_writing_past_its_deadline_to_a_paused_reader_times_out_once_read_on() -> None:
    spec = SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key="exec-deadline-paused-reader",
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
    )
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(spec)
        try:
            handle = await session.exec_start(_ENDLESS_WRITER_ARGV, timeout_seconds=_TIMEOUT_SECONDS)
            output = handle.output
            await anext(output)
            await asyncio.sleep(_READER_PAUSE_SECONDS)  # noqa: TID251 — the paused reader under test
            with pytest.raises(SandboxExecTimeoutError) as raised:
                await asyncio.wait_for(_read_to_end(output), _TIMEOUT_RAISED_SECONDS)
            assert getattr(raised.value, "__notes__", []) == []
        finally:
            await session.destroy()
