"""A preset's output stage holds on the result that reappears after its base parks and resumes.

The preset sits at the door around a driver that binds its own continuation and asks; the driver's
resumed terminal is the preset's result, so the preset's ``output_schema`` judges it. A preset a
driver calls over an asking tool maps the same value whether the ask answers live or parks and is
resumed: the value the driver hands the waiting call is the one a sync ask returns.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from tai42_contract.access_control import reset_request_user_id, set_request_user_id
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    InteractionResponse,
    SuspendedInteraction,
    open_nested_run_face,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.presets import PresetOutputStage, preset_output_failed_outcome
from tai42_contract.tools import TOOL_META_PAUSES
from tai42_kit.interactions.park_marker import read_suspended_interaction_marker
from tests._helpers import await_add_event

from tai42_skeleton.app.instance import app
from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import continuation_dispatch as continuation_dispatch_module
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions import kill as kill_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import interactions as ops
from tai42_skeleton.tools.binding.surface import remove_surface_tool


@pytest.fixture(autouse=True)
def _store_configured(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            remove_surface_tool(app._fast_mcp, tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def _wire(monkeypatch, fake) -> SimpleNamespace:
    from contextlib import asynccontextmanager

    from tai42_skeleton.access_control.settings import AccessControlSettings
    from tai42_skeleton.authz import execution as execution_module

    settings = InteractionsSettings()

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    for module in (ops, helper_module, continuation_module, kill_module):
        monkeypatch.setattr(module, "client_ctx", _ctx)
        monkeypatch.setattr(module, "interactions_settings", lambda: settings)
    monkeypatch.setattr(execution_module, "access_control_settings", lambda: AccessControlSettings(enable=False))
    return SimpleNamespace(settings=settings, store=InteractionStore(settings.key_prefix), fake=fake)


def _terminal_stub(output: Any):
    async def _stub(identity, fingerprint, tool, interaction_id, answer, *args, **kwargs):
        return output

    return _stub


async def _drain() -> None:
    for _ in range(50):
        tasks = {t for t in continuation_dispatch_module._CONTINUATION_TASKS if not t.done()}
        if not tasks:
            break
        await asyncio.wait(tasks, timeout=0.1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_a_door_preset_output_schema_judges_the_resumed_terminal(monkeypatch, fake_redis):
    _wire(monkeypatch, fake_redis)
    delivered: list[dict[str, Any]] = []

    async def _capture(store, **kwargs):
        delivered.append(kwargs)

    monkeypatch.setattr(continuation_module, "_deliver_terminal", _capture)
    monkeypatch.setattr(continuation_module, "_clear_due", lambda *a, **k: asyncio.sleep(0))
    deadline = datetime.now(UTC) + timedelta(hours=1)

    async with app.app_context(Manifest.model_validate({})):

        @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
        async def asking_driver(x: int) -> SuspendedInteraction:
            """A run that binds its own continuation, then asks."""
            token = set_resume_continuation_tool("resume_tool")
            try:
                parked = await helper_module.ask("proceed?", mode="async", expiry_at=deadline)
                assert isinstance(parked, SuspendedInteraction)
                return parked
            finally:
                reset_resume_continuation_tool(token)

        schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
        await app.preset_manager.register("po", "asking_driver", {}, [], "a parking preset", output_schema=schema)

        id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
        try:
            parked = await app.tools.run_tool("po", {"x": 5})
        finally:
            reset_execution_identity(id_token)
        assert isinstance(parked, SuspendedInteraction)

        monkeypatch.setattr(continuation_module, "_run_continuation", _terminal_stub({"ok": 7}))
        token = set_request_user_id("answerer-x")
        try:
            await ops.answer_interaction(parked.interaction_id, "go")
        finally:
            reset_request_user_id(token)
        await _drain()

    assert len(delivered) == 1
    assert delivered[0]["status"] == PARK_COMPLETION_FAILED
    message = (
        "preset 'po' output does not match its output_schema: value does not match schema at $: "
        "'answer' is a required property"
    )
    assert delivered[0]["outcome"] == preset_output_failed_outcome("po", message)
    assert delivered[0].get("reply") is None


async def test_a_park_under_an_adopted_frame_carries_the_stage_on_the_in_process_sentinel_only(monkeypatch, fake_redis):
    _wire(monkeypatch, fake_redis)
    deadline = datetime.now(UTC) + timedelta(hours=1)
    schema = {"type": "object"}
    adopted: list[list[PresetOutputStage]] = []

    async with app.app_context(Manifest.model_validate({})):

        @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
        async def nested_asker(x: int) -> SuspendedInteraction:
            """A nested run resuming through its parent's continuation: a face of its own, then an ask."""
            token = open_nested_run_face()
            try:
                parked = await helper_module.ask("proceed?", mode="async", expiry_at=deadline)
                assert isinstance(parked, SuspendedInteraction)
                return parked
            finally:
                reset_resume_continuation_tool(token)

        @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
        async def adopting_driver() -> SuspendedInteraction:
            """A driver that runs a nested run under its own continuation and adopts its park."""
            token = set_resume_continuation_tool("resume_tool")
            try:
                parked = await app.tools.run_tool("nested", {"x": 1})
            finally:
                reset_resume_continuation_tool(token)
            assert isinstance(parked, SuspendedInteraction)
            adopted.append(list(parked.output_stages))
            return parked

        await app.preset_manager.register("nested", "nested_asker", {}, [], "a nested preset", output_schema=schema)

        id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
        try:
            await app.tools.run_tool("adopting_driver", {})
            async with Client(app._fast_mcp) as client:
                result = await client.call_tool("adopting_driver", {})
        finally:
            reset_execution_identity(id_token)

    stage = PresetOutputStage(preset="nested", output_schema=schema)
    assert adopted == [[stage], [stage]]
    marker = read_suspended_interaction_marker(result.structured_content)
    assert marker is not None
    assert "output_stages" not in json.dumps(result.structured_content)


async def test_a_preset_over_an_asking_tool_maps_the_same_value_live_and_resumed(monkeypatch, fake_redis):
    wired = _wire(monkeypatch, fake_redis)
    params = {"button_payload": "P1"}
    deadline = datetime.now(UTC) + timedelta(hours=1)
    seen_by_driver: list[Any] = []
    handed_back: list[Any] = []

    def _response(interaction_id: str) -> InteractionResponse:
        return InteractionResponse(
            interaction_id=interaction_id, answer="go", answered_by="u-1", answered_at=datetime.now(UTC), params=params
        )

    async def _answer_live() -> None:
        interaction_id, group_id = await await_add_event(fake_redis, wired.store)
        assert await wired.store.record_answer(fake_redis, _response(interaction_id), group_id, reply_ttl=60)

    async with app.app_context(Manifest.model_validate({})):

        @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
        async def asking_tool(mode: str) -> Any:
            """Ask, and return what the ask tool returns: the ``AskAnswer`` JSON, or the park."""
            if mode == "async":
                result = await helper_module.ask("proceed?", mode="async", expiry_at=deadline)
            else:
                result = await helper_module.ask("proceed?", mode="sync", timeout=5)
            if isinstance(result, SuspendedInteraction):
                return result
            return result.model_dump(mode="json")

        @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
        async def asking_driver(mode: str) -> Any:
            """A run that binds its own continuation and calls the preset; it reads the call's result."""
            token = set_resume_continuation_tool("resume_tool")
            try:
                result = await app.tools.run_tool("pa", {"mode": mode})
            finally:
                reset_resume_continuation_tool(token)
            seen_by_driver.append(result)
            return result

        @app.tools.tool(force=True)
        async def resume_tool(interaction_id: str, ask_result: Any) -> Any:
            """The driver's continuation: hands the waiting call ``ask_result`` unchanged."""
            handed_back.append(ask_result)
            return ask_result

        await app.preset_manager.register("pa", "asking_tool", {}, [], "an asking preset", output_map="{seen: .}")

        id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
        try:
            answerer = asyncio.create_task(_answer_live())
            await app.tools.run_tool("asking_driver", {"mode": "sync"})
            await answerer
            parked = await app.tools.run_tool("asking_driver", {"mode": "async"})
        finally:
            reset_execution_identity(id_token)
        assert isinstance(parked, SuspendedInteraction)

        monkeypatch.setattr(continuation_module, "_deliver_terminal", lambda *a, **k: asyncio.sleep(0))
        monkeypatch.setattr(continuation_module, "_clear_due", lambda *a, **k: asyncio.sleep(0))
        state = await wired.store.get_state(fake_redis, parked.interaction_id)
        assert state is not None
        response = _response(parked.interaction_id)
        assert await wired.store.record_answer(
            fake_redis,
            response,
            state.group_id,
            reply_ttl=60,
            continuation_due_ttl=86400,
            continuation_first_attempt_at_ms=0,
        )
        await continuation_dispatch_module.fire_continuation_after_claim(
            fake_redis, wired.store, state.request, response
        )
        await _drain()

    live = {"seen": {"answer": "go", "reply_to": None, "params": params}}
    assert seen_by_driver[0] == live
    assert handed_back == [live]
