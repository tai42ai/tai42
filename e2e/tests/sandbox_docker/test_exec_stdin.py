"""Interactive stdin on the REAL Docker provider over a live engine: a command's exit ends its output.

``head -c 1`` takes one byte of a stdin write far larger than every buffer on its way
and exits. The engine then stops reading the stdin, so the write still buffered must not hold
the stream open: the output ends with the command's exit code within the exec's timeout, and
the write in flight raises the closed-stream error.

Docker-gated: the parent conftest keeps this module out of collection unless
``TAI_E2E_SANDBOX_DOCKER=1``, and it skips LOUDLY when ``SANDBOX_DOCKER_TEST_HOST``
names no engine.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from tai42_contract.sandbox import SandboxError, SandboxSessionSpec, SandboxStreamChunk, SandboxStreamExit
from tai42_kit.sandbox.sibling_kill_handle import STDIN_STREAM_CLOSED

from ._support import TEST_IMAGE, open_sandbox, requires_engine

# A stdin write far past every buffer between the provider and the command: the attach
# connection's socket and transport buffers, and those of each proxy that forwards the engine's
# published port, which together take several megabytes.
_LARGE_STDIN = b"y" * (64 << 20)

# The exec's timeout: its output ends at the command's exit, so only a provider that holds the
# stream open on the unsent stdin runs past it.
_TIMEOUT_SECONDS = 10.0


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_an_interactive_exec_whose_command_exits_under_a_large_stdin_write_ends_with_its_exit_code() -> None:
    spec = SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key="exec-stdin-unread",
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
    )
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(spec)
        try:
            handle = await session.exec_start(["head", "-c", "1"], timeout_seconds=_TIMEOUT_SECONDS)
            write = asyncio.ensure_future(handle.write_stdin(_LARGE_STDIN))
            try:
                items = [item async for item in handle.output]
                ended, _ = await asyncio.wait({write}, timeout=_TIMEOUT_SECONDS)
            finally:
                write.cancel()
                await asyncio.wait({write})
            assert items == [SandboxStreamChunk(stream="stdout", data=b"y"), SandboxStreamExit(exit_code=0)]
            assert write in ended, "the stdin write was still blocked after the output ended"
            with pytest.raises(SandboxError, match=f"^{re.escape(STDIN_STREAM_CLOSED)}$"):
                write.result()
        finally:
            await session.destroy()
