"""A ``free`` ask given a ``schema`` stores it, and a resume whose answer does not conform is refused."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import (
    AnswerMismatchError,
    ResumeItem,
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_contract.tools import tool_call_frame
from tai42_kit.utils.state_context import state_context

from tai42_skeleton.app import instance
from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import InteractionStore, ask
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding.surface import remove_surface_tool

_MOD = "tests.app._fixtures.resume_tool"
_INTEGER = {"type": "integer"}


def _context() -> StateContext:
    candidates = SubjectCandidates(target_kind="tool", target_name="t", by_kind={"person": "pA"})
    return StateContext(door="conversation", candidates=candidates, actor="u-1", turn_id="t-1")


@pytest.fixture(autouse=True)
def settings(monkeypatch, fake_redis, fake_client_ctx) -> InteractionsSettings:
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    settings = InteractionsSettings()
    for module in (helper_module, visit_module, continuation_module):
        monkeypatch.setattr(module, "client_ctx", fake_client_ctx)
        monkeypatch.setattr(module, "interactions_settings", lambda: settings)
    monkeypatch.setattr(visit_module, "interactions_store_configured", lambda: True)

    @asynccontextmanager
    async def _fake_bind(identity, *, bound_fingerprint=""):
        yield

    monkeypatch.setattr(continuation_module, "bind_execution_identity", _fake_bind)
    return settings


async def _clear_server() -> None:
    provider = instance.app._fast_mcp.local_provider
    for tool in list(await provider.list_tools()):
        remove_surface_tool(instance.app._fast_mcp, tool.name)


@pytest.fixture(autouse=True)
def _clean_server():
    asyncio.run(_clear_server())
    yield
    asyncio.run(_clear_server())


async def _park_free_caller_ask() -> SuspendedInteraction:
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    try:
        with tool_call_frame(), state_context(_context()):
            parked = await ask(
                "pick a number",
                answer_format="free",
                schema=_INTEGER,
                to="caller",
                mode="async",
                expiry_at=datetime.now(UTC) + timedelta(hours=1),
            )
    finally:
        reset_execution_identity(id_token)
        reset_resume_continuation_tool(tool_token)
    assert isinstance(parked, SuspendedInteraction)
    return parked


async def test_a_free_ask_stores_its_schema(settings, fake_redis):
    parked = await _park_free_caller_ask()

    stored = await InteractionStore(settings.key_prefix).get_state(fake_redis, parked.interaction_id)

    assert stored is not None
    assert stored.request.format_payload == {"schema": _INTEGER}


async def test_a_resume_with_an_answer_outside_the_free_schema_is_refused(settings):
    parked = await _park_free_caller_ask()

    await _clear_server()
    manifest = Manifest.model_validate({"tools": [{"title": "resume", "module": _MOD}]})
    async with instance.app.app_context(manifest):
        with state_context(_context()), pytest.raises(AnswerMismatchError) as refused:
            await visit_module.visit(
                target_name="t",
                cancel=[],
                resume=[ResumeItem(id=parked.interaction_id, payload="not an integer")],
                start=None,
                extras={},
            )

    assert str(refused.value) == "answer does not match schema: 'not an integer' is not of type 'integer'"
