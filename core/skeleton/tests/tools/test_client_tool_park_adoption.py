"""The in-process client-tool runnable's park-ADOPTION rule.

A tool that async-parks returns the ``SuspendedInteraction`` sentinel; this seam turns it
into the reserved park marker, which is what makes the CALLING run (an agent loop) park on
that interaction. A park has exactly one resume owner, so the conversion is allowed only
when the park was raised under the continuation bound here — and the marker it produces
carries that owner onward, because the driver that finally claims the park reads the
serialized ToolMessage, not the sentinel.

A park a NESTED run owns — a driver tool that bound its own resume continuation, or an
agent run parked at its tool face — is resumed on that run's own path and would never
resume this one, so it is never adopted. What happens instead depends on the caller: a
CHAINED dispatch parks on the chained KEY (the call), so the nested run keeps its park and
its terminal re-enters the caller through the chain's delivery tool; an unchained one is
refused as a model-visible ``ToolException`` instead of suspending the caller behind a
resume that never comes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain_core.tools import ToolException
from tai42_contract.interactions import (
    SuspendedInteraction,
    chained_park_context,
    read_suspended_interaction_marker,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
)

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


async def _run_parking_tool(
    resume_owner: str | None, *, bound: str | None, chained: str | None = None, expiry_at: datetime | None = None
):
    """Invoke a client tool that returns a park sentinel owned by ``resume_owner``, with
    ``bound`` as this run's resume continuation and (optionally) ``chained`` as the chained
    key its caller bound around the dispatch."""
    async with app.app_context(Manifest.model_validate({})):

        @app.tools.tool(force=True)
        async def parks(q: str) -> SuspendedInteraction:
            """A tool whose call async-parked its caller."""
            return SuspendedInteraction(interaction_id="i1", resume_owner=resume_owner, expiry_at=expiry_at)

        tool_obj = await app.tools.get_tool("parks")
        runnable = app._tool_binding._client_runnable(tool_obj)
        token = set_resume_continuation_tool(bound)
        completion = (
            set_park_completion("deliver_chained_park", chained_park_context(chained, (None, None)))
            if chained is not None
            else None
        )
        try:
            return await runnable(q="x")
        finally:
            if completion is not None:
                reset_park_completion(completion)
            reset_resume_continuation_tool(token)


def test_a_park_this_run_owns_becomes_the_park_marker():
    # The ask ran under THIS run's binding, so this run is the park's resume owner: the
    # sentinel converts to the reserved marker its park middleware interrupts on.
    result = asyncio.run(_run_parking_tool("agent_resume", bound="agent_resume"))
    marker = read_suspended_interaction_marker(result)
    assert marker is not None
    assert marker["interaction_id"] == "i1"


def test_the_marker_carries_the_owner_onward():
    # The claim point reads the marker off a serialized ToolMessage and never sees the
    # sentinel, so the owner has to ride the wire form for it to be checkable there too.
    result = asyncio.run(_run_parking_tool("agent_resume", bound="agent_resume"))
    marker = read_suspended_interaction_marker(result)
    assert marker is not None
    assert marker["resume_owner"] == "agent_resume"


def test_a_nested_drivers_park_is_refused_to_the_model():
    # A nested driver bound its own continuation for the ask, so the platform resumes THAT
    # driver. Refused as a ToolException — the class the agent loop's error middleware turns
    # into a model-visible error ToolMessage — never converted to a park marker.
    with pytest.raises(ToolException) as caught:
        asyncio.run(_run_parking_tool("nested_driver_resume", bound="agent_resume"))
    message = str(caught.value)
    assert "parks" in message
    assert "different run's resume binding" in message
    # The refusal names NEITHER owner: echoing the bound continuation would hand a model the
    # exact string to name for a claim, so it stays store-name-free.
    assert "'nested_driver_resume'" not in message
    assert "agent_resume" not in message


def test_the_refusal_is_not_double_prefixed_as_a_tool_failure():
    # A deliberate refusal is already worded for the model and already names the tool: it
    # carries its own message, never the seam's "Error calling tool" body-failure prefix.
    with pytest.raises(ToolException) as caught:
        asyncio.run(_run_parking_tool("nested_driver_resume", bound="agent_resume"))
    assert str(caught.value).startswith("Tool 'parks' cannot be used inside this run")
    assert "Error calling tool" not in str(caught.value)


def test_a_nested_runs_ownerless_park_is_refused_to_the_model():
    # A park surfaced with no adoptable owner (an agent run parked at its tool face) is
    # refused the same way — the nested run drives its own resume.
    with pytest.raises(ToolException, match="no adoptable owner"):
        asyncio.run(_run_parking_tool(None, bound="agent_resume"))


def test_an_unbound_caller_is_refused_an_ownerless_park_too():
    # ``None`` is not adoptable by ANY caller: an ask that parks always stamps the
    # continuation it was raised under, so an ownerless sentinel is one no ask minted here.
    # An unbound caller has no continuation the platform could fire either way.
    with pytest.raises(ToolException, match="no adoptable owner"):
        asyncio.run(_run_parking_tool(None, bound=None))


def test_a_real_ask_user_park_is_adoptable_by_the_binding_that_raised_it():
    # End to end on the PRODUCER side: the sentinel is minted by the real ``ask_user`` under a
    # bound continuation (not hand-built), so this pins that what the helper stamps is exactly
    # what the adoption seam accepts — the two halves cannot drift apart.
    async def run() -> Any:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def asks(q: str) -> Any:
                """A tool that async-parks through the real ask_user helper."""
                from tai42_skeleton.authz.execution_identity import (
                    reset_execution_identity,
                    set_execution_identity,
                )
                from tai42_skeleton.authz.identity import CallerIdentity
                from tai42_skeleton.interactions import ask_user

                # The identity an async park records its continuation under, bound around the
                # ask ONLY: bound any wider it would send the dispatch through the live-fire
                # entitlement gate, which is not what this test exercises.
                identity = set_execution_identity(CallerIdentity(user_id="svc", execution_key_fingerprint="fp"))
                try:
                    return await ask_user(q, mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
                finally:
                    reset_execution_identity(identity)

            tool_obj = await app.tools.get_tool("asks")
            runnable = app._tool_binding._client_runnable(tool_obj)
            token = set_resume_continuation_tool("agent_resume")
            try:
                return await runnable(q="proceed?")
            finally:
                reset_resume_continuation_tool(token)

    marker = read_suspended_interaction_marker(_with_interactions_store(run))
    assert marker is not None
    # The helper stamped the bound continuation, the seam accepted it, and the wire form
    # carries it on to whatever claims the park.
    assert marker["resume_owner"] == "agent_resume"


def test_a_real_agent_tool_face_park_is_refused_by_an_adopting_run():
    # End to end on the OTHER producer: an agent run parked as a TOOL of another agent. The
    # tool face mints the sentinel with no owner (the parked run holds its own resume state),
    # so the adopting run is refused instead of overwriting that run's claim.
    async def run() -> Any:
        manifest = Manifest.model_validate(
            {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["parking"]}]}
        )
        async with app.app_context(manifest):
            tool_obj = await app.tools.get_tool("parking")
            runnable = app._tool_binding._client_runnable(tool_obj)
            token = set_resume_continuation_tool("agent_resume")
            try:
                return await runnable(text="hi")
            finally:
                reset_resume_continuation_tool(token)

    with pytest.raises(ToolException, match="no adoptable owner"):
        asyncio.run(run())


def _with_interactions_store(run: Any) -> Any:
    """Drive ``run`` with the interactions store pointed at an in-memory fakeredis, so the
    real ``ask_user`` persists its park without a live server."""
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


def test_a_chained_dispatch_parks_on_the_call_not_the_nested_interaction():
    # The caller bound a chained completion around this dispatch, so the nested run's park is
    # not adopted and not refused: this run parks on the chained KEY, and the nested run's
    # terminal re-enters it through that binding's delivery tool.
    deadline = datetime(2030, 1, 1, tzinfo=UTC)
    result = asyncio.run(
        _run_parking_tool(
            "nested_driver_resume", bound="agent_resume", chained="tai42:chained-park:k1", expiry_at=deadline
        )
    )
    marker = read_suspended_interaction_marker(result)
    assert marker is not None
    assert marker["interaction_id"] == "tai42:chained-park:k1"
    # Owned by THIS run's continuation: the chained park is genuinely this run's — it waits on
    # the CALL — so the claim point downstream reaches the same verdict.
    assert marker["resume_owner"] == "agent_resume"
    # The chained park INHERITS the nested ask's horizon: the deadline rides through unchanged.
    assert marker["expiry_at"] == deadline.isoformat()


def test_a_chained_dispatch_still_adopts_this_runs_own_park():
    # An ask raised under THIS run's binding inside a chained dispatch is still the run's own
    # park — the chained key names the CALL, and nothing about it is this ask.
    result = asyncio.run(_run_parking_tool("agent_resume", bound="agent_resume", chained="tai42:chained-park:k1"))
    marker = read_suspended_interaction_marker(result)
    assert marker is not None
    assert marker["interaction_id"] == "i1"
    assert marker["resume_owner"] == "agent_resume"


def test_a_chained_dispatch_chains_an_ownerless_nested_park_too():
    # A nested RUN parked at its tool face names no adoptable owner; chaining never adopts its
    # park, it waits on the run's terminal, so the ownerless receipt chains like any other.
    result = asyncio.run(_run_parking_tool(None, bound="agent_resume", chained="tai42:chained-park:k1"))
    marker = read_suspended_interaction_marker(result)
    assert marker is not None
    assert marker["interaction_id"] == "tai42:chained-park:k1"
    assert marker["resume_owner"] == "agent_resume"
