"""Rollback's bound-route re-check, driven through ``rollback_preset``: a rollback to a version that
cannot bind a conversation route bound to the preset — or to a preset composing it — is a 409 naming
the version rolled back to, and the active version is not re-pointed."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.conversations import ApiConversationRoute, ConversationRoute, ConversationRouteCreate
from tai42_contract.presets import PresetBody

from tai42_skeleton.app import instance
from tai42_skeleton.operations import ConflictError
from tai42_skeleton.operations import presets as preset_ops

from .conftest import _create, _manifest


class _RoutesManager:
    def __init__(self) -> None:
        self.routes: dict[str, ConversationRoute] = {}

    async def list_routes(self) -> tuple[dict[str, ConversationRoute], int]:
        return dict(self.routes), 0


@pytest.fixture
def routes(monkeypatch) -> _RoutesManager:
    import tai42_skeleton.conversations as conv_pkg

    manager = _RoutesManager()
    monkeypatch.setattr(conv_pkg, "get_conversations_manager", lambda: manager)
    return manager


def _route(route_name: str, target_name: str) -> ApiConversationRoute:
    return ApiConversationRoute(
        route_name=route_name,
        door="api",
        target_kind="tool",
        target_name=target_name,
        execution_key="svc",
        callback_url="https://example.com/cb",
        execution_key_fingerprint="fp-1",
    )


async def _weather_validator(route: ConversationRouteCreate, candidate: PresetBody | None) -> list[str]:
    if candidate is not None and candidate.fixed_kwargs.get("units") == "bad":
        return [f"target {route.target_name!r} bakes bad units"]
    return []


async def _plan_validator(route: ConversationRouteCreate, candidate: PresetBody | None) -> list[str]:
    if candidate is None:
        return []
    lines = []
    for composed in candidate.fixed_kwargs.get("plan", {}).get("refs", []):
        composed_body = await instance.app.presets.store.get_active_body(composed)
        if composed_body.fixed_kwargs.get("units") == "bad":
            lines.append(f"target {route.target_name!r} composes {composed!r} with bad units")
    return lines


async def _p_with_good_version_2_active() -> None:
    await _create("p", fixed_kwargs={"units": "bad"})
    await preset_ops.save_version(
        name="p",
        fixed_kwargs={"units": "good"},
        extensions=None,
        output_schema=None,
        output_schema_provided=False,
        description=None,
    )


async def _assert_still_on_version_2() -> None:
    record = await instance.app.presets.store.get_preset("p")
    assert record.active_version == 2
    assert (await instance.app.presets.store.get_active_body("p")).fixed_kwargs == {"units": "good"}


def test_a_rollback_to_a_version_that_cannot_bind_a_bound_route_is_refused(pg, routes: _RoutesManager) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app.conversations.register_target_validator("tool", "weather", _weather_validator)
            await _p_with_good_version_2_active()
            routes.routes["rp"] = _route("rp", "p")

            with pytest.raises(ConflictError) as refused:
                await preset_ops.rollback_preset(name="p", version=1)
            assert str(refused.value) == (
                "preset 'p' cannot be rolled back to version 1 — that version would leave conversation route(s) "
                "unable to bind: route 'rp', bound to preset 'p': version 1 cannot bind it "
                "(target 'p' bakes bad units)"
            )
            await _assert_still_on_version_2()

    asyncio.run(run())


def test_a_rollback_to_a_version_that_breaks_a_composing_route_is_refused(pg, routes: _RoutesManager) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app.conversations.register_target_validator("tool", "plan_tool", _plan_validator)
            await _p_with_good_version_2_active()
            plan: dict[str, Any] = {"refs": ["p"]}
            await _create("q", base_tool="plan_tool", fixed_kwargs={"plan": plan})
            routes.routes["rq"] = _route("rq", "q")

            with pytest.raises(ConflictError) as refused:
                await preset_ops.rollback_preset(name="p", version=1)
            assert str(refused.value) == (
                "preset 'p' cannot be rolled back to version 1 — that version would leave conversation route(s) "
                "unable to bind: route 'rq', bound to preset 'q': preset 'q' would no longer bind with version 1 "
                "(target 'q' composes 'p' with bad units)"
            )
            await _assert_still_on_version_2()

    asyncio.run(run())
