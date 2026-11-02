"""The files an exec keeps in the session container's ``/tmp``, on the REAL Docker provider over a live engine.

An exec whose command leaves a process holding its output ends once that process has ended,
and leaves no output identity of its own behind in ``/tmp``: the next exec in the session lists
only its own.

Docker-gated: the parent conftest keeps this module out of collection unless
``TAI_E2E_SANDBOX_DOCKER=1``, and it skips LOUDLY when ``SANDBOX_DOCKER_TEST_HOST``
names no engine.
"""

from __future__ import annotations

import time

import pytest
from tai42_contract.sandbox import SandboxSessionSpec

from ._support import TEST_IMAGE, open_sandbox, requires_engine


@requires_engine
@pytest.mark.needs("no-stack", "helper:docker-engine")
async def test_an_exec_whose_output_a_process_it_started_held_leaves_no_identity_behind() -> None:
    spec = SandboxSessionSpec(
        image=TEST_IMAGE,
        workspace_key="exec-files-identity",
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
    )
    async with open_sandbox() as sandbox:
        session = await sandbox.create_session(spec)
        try:
            # The holder inherits the exec's stdout and outlives the command; the exec ends once
            # it has ended by itself.
            started = time.monotonic()
            held = await session.exec(["sh", "-c", "setsid sh -c 'sleep 1' &"], timeout_seconds=30)
            took = time.monotonic() - started
            assert held.exit_code == 0
            assert took >= 1
            listing = await session.exec(["sh", "-c", "ls /tmp | grep '^tai-exec\\..*\\.out$'"], timeout_seconds=30)
            assert listing.exit_code == 0
            assert len(listing.stdout.splitlines()) == 1, listing.stdout
        finally:
            await session.destroy()
