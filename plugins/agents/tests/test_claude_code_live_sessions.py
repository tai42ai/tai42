"""The worker's live ``claude_code`` session cache holds a session only as long as the sandbox does."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import Iterator

import pytest
from tai42_contract.sandbox import SandboxSessionSpec

import tai42_agents.claude_code.live_sessions as live_sessions_module
from tai42_agents.claude_code.live_sessions import live_session

from ._sandbox_fake import FakeSandbox, make_fake_sandbox

_KEY = "live-sessions"


def _spec() -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image="registry.example/lean@sha256:" + "a" * 64,
        workspace_key=_KEY,
        durability="persistent",
        network="egress",
        ttl_seconds=300,
    )


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    live_sessions_module.LIVE_SESSIONS.clear()
    yield
    live_sessions_module.LIVE_SESSIONS.clear()


@pytest.fixture
def sandbox() -> Iterator[FakeSandbox]:
    sandbox = make_fake_sandbox()
    yield sandbox
    sandbox.dispose()


def test_a_session_the_sandbox_holds_is_kept_and_reused(sandbox: FakeSandbox) -> None:
    first_id = asyncio.run(live_session(sandbox, _spec(), _KEY)).id
    gc.collect()

    assert live_sessions_module.LIVE_SESSIONS[_KEY].id == first_id
    assert asyncio.run(live_session(sandbox, _spec(), _KEY)).id == first_id


def test_a_session_the_sandbox_reaped_leaves_the_cache(sandbox: FakeSandbox) -> None:
    first_id = asyncio.run(live_session(sandbox, _spec(), _KEY)).id

    asyncio.run(sandbox.destroy_session_via_reap(first_id))
    gc.collect()

    assert _KEY not in live_sessions_module.LIVE_SESSIONS
    assert asyncio.run(live_session(sandbox, _spec(), _KEY)).id != first_id


def test_a_dropped_sandbox_takes_its_sessions_out_of_the_cache() -> None:
    sandbox = make_fake_sandbox()
    try:
        asyncio.run(live_session(sandbox, _spec(), _KEY))
    finally:
        sandbox.dispose()

    del sandbox
    gc.collect()

    assert _KEY not in live_sessions_module.LIVE_SESSIONS
