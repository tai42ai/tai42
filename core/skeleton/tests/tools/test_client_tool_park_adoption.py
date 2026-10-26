"""The client-tool handle hands a park back BY TYPE, unadapted, with the owner its producer stamped.

A tool that async-parks returns the ``SuspendedInteraction`` sentinel; the handle returns it as it
is — adopting it (this run's own park, a chained park on the call, or a refusal) is the agent
engine's adapter's decision, made over the owner the sentinel carries. So the producers are pinned
here: the real ``ask`` stamps the continuation bound around it, an agent run parked at its tool face
stamps none, and a nested driver's park keeps its own owner. The handle also opens the tool's own
call frame, so an ask it raises records the tool in ``asked_by``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tai42_contract.interactions import (
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.tools import TOOL_META_PAUSES, current_call_chain, tool_call_frame

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding.surface import remove_surface_tool


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            remove_surface_tool(app._fast_mcp, tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def test_the_handle_opens_the_push_frame_of_the_tool() -> None:
    seen: dict[str, tuple[str, ...]] = {}

    async def _run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def records_chain(q: str) -> str:
                """Records the ambient call chain the handle dispatches under."""
                seen["chain"] = current_call_chain()
                return "ok"

            [handle] = await app.tools.get_client_tools(["records_chain"])
            await handle.invoke({"q": "x"})

    asyncio.run(_run())
    assert seen["chain"] == ("records_chain",)


def test_a_nested_drivers_park_is_returned_with_its_own_owner() -> None:
    # Whose park it is decides nothing here: the handle returns the sentinel as the body made it.
    async def run() -> Any:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
            async def drives(q: str) -> SuspendedInteraction:
                """A tool whose nested driver parked under its own continuation."""
                return SuspendedInteraction(interaction_id="i-nested", resume_owner="nested_driver_resume")

            [handle] = await app.tools.get_client_tools(["drives"])
            token = set_resume_continuation_tool("agent_resume")
            try:
                return await handle.invoke({"q": "x"})
            finally:
                reset_resume_continuation_tool(token)

    result = asyncio.run(run())
    assert isinstance(result, SuspendedInteraction)
    assert result.resume_owner == "nested_driver_resume"


def test_a_real_ask_park_carries_the_continuation_bound_around_it() -> None:
    async def run() -> Any:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
            async def asks(q: str) -> Any:
                """A tool that async-parks through the real ask helper."""
                from tai42_skeleton.authz.execution_identity import (
                    reset_execution_identity,
                    set_execution_identity,
                )
                from tai42_skeleton.authz.identity import CallerIdentity
                from tai42_skeleton.interactions import ask

                # The identity an async park records its continuation under, bound around the
                # ask ONLY: bound any wider it would send the dispatch through the live-fire
                # entitlement gate, which is not what this test exercises.
                identity = set_execution_identity(CallerIdentity(user_id="svc", execution_key_fingerprint="fp"))
                try:
                    return await ask(q, mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
                finally:
                    reset_execution_identity(identity)

            [handle] = await app.tools.get_client_tools(["asks"])
            token = set_resume_continuation_tool("agent_resume")
            try:
                # The outer door frame that mints the run's delivery identity a parkable run
                # always carries (a real dispatch always opens one before the tool body runs).
                with tool_call_frame():
                    return await handle.invoke({"q": "proceed?"})
            finally:
                reset_resume_continuation_tool(token)

    result = _with_interactions_store(run)
    assert isinstance(result, SuspendedInteraction)
    assert result.resume_owner == "agent_resume"
    assert result.interaction_ids == [result.interaction_id]
    assert result.caller_interaction_ids == []


def test_an_agent_run_parked_at_its_tool_face_carries_no_owner() -> None:
    # The parked run holds its own resume state, so the sentinel its tool face mints names no
    # owner: no caller may adopt it.
    async def run() -> Any:
        manifest = Manifest.model_validate(
            {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["parking"]}]}
        )
        async with app.app_context(manifest):
            [handle] = await app.tools.get_client_tools(["parking"])
            token = set_resume_continuation_tool("agent_resume")
            try:
                return await handle.invoke({"text": "hi"})
            finally:
                reset_resume_continuation_tool(token)

    result = asyncio.run(run())
    assert isinstance(result, SuspendedInteraction)
    assert result.resume_owner is None


def _with_interactions_store(run: Any) -> Any:
    """Drive ``run`` with the interactions store pointed at an in-memory fakeredis, so the
    real ``ask`` persists its park without a live server."""
    import contextlib

    from fakeredis import aioredis

    from tai42_skeleton.interactions import helper as helper_module
    from tai42_skeleton.interactions.settings import InteractionsSettings

    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield redis

    settings = InteractionsSettings(redis={"redis_url": "redis://fake"})  # type: ignore[arg-type]
    real_ctx, real_settings = helper_module.client_ctx, helper_module.interactions_settings
    helper_module.client_ctx = fake_client_ctx  # type: ignore[assignment]
    helper_module.interactions_settings = lambda: settings  # type: ignore[assignment]
    try:
        return asyncio.run(run())
    finally:
        helper_module.client_ctx = real_ctx  # type: ignore[assignment]
        helper_module.interactions_settings = real_settings  # type: ignore[assignment]
