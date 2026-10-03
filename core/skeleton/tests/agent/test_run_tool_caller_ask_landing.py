"""The run-tool face's caller-ask landing fail-fast.

``agent/binding.run_impl`` is where a preset over an agent and a direct agent-run-tool target both
resolve their EXACT tool set (after the preset transform). It is the ONE site that fails a run fast —
before ``agent.run``, so no side effect — when the run WILL bind the caller-ask tool and the door that
started it declared no landing. The guarantee itself lives at the park seam; this is the optimization
that spares a doomed drive.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.interactions import CallerAskLanding, RunTerminalFailed, declare_caller_ask_landing

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

from .conftest import _fixture_flag


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["asker_agent"]}]}
    )


def _asker_run_called() -> bool:
    return bool(_fixture_flag("tests.agent._fixtures", "asker_run_called"))


def test_run_tool_face_fails_fast_when_exact_set_binds_ask_and_no_landing() -> None:
    # Neutral consumer: injected ``tool_names=["ask"]`` + an ABSENT landing → the run-tool face
    # fails with the typed run failure before ``agent.run``, naming the route and the caller-ask tool.
    async def run() -> None:
        async with app.app_context(_manifest()):
            with (
                declare_caller_ask_landing(CallerAskLanding(can_land=False, label="chat")),
                pytest.raises(RunTerminalFailed) as excinfo,
            ):
                await app.tools.run_tool("asker_agent", {"tool_names": ["ask"]})
            outcome: Any = excinfo.value.outcome
            assert outcome["route"] == "chat"
            assert outcome["tool"] == "ask"

    asyncio.run(run())
    assert _asker_run_called() is False  # no run side effect happened


def test_run_tool_face_runs_when_landing_present() -> None:
    # Neutral consumer, run-tool face: with the landing PRESENT the same injected ask is not
    # refused — the run proceeds.
    async def run() -> None:
        async with app.app_context(_manifest()):
            with declare_caller_ask_landing(CallerAskLanding(can_land=True, label="chat")):
                assert await app.tools.run_tool("asker_agent", {"tool_names": ["ask"]}) == "ran"

    asyncio.run(run())
    assert _asker_run_called() is True


def test_run_tool_face_makes_no_claim_when_no_landing_declared() -> None:
    # Neutral consumer, run-tool face: a door that declared nothing never refuses — the run
    # proceeds even with an injected ask.
    async def run() -> None:
        async with app.app_context(_manifest()):
            assert await app.tools.run_tool("asker_agent", {"tool_names": ["ask"]}) == "ran"

    asyncio.run(run())
    assert _asker_run_called() is True
