"""Bound-route re-check, composing routes: a write of preset ``P`` re-checks every route bound to a
preset that COMPOSES ``P`` as a tool too, resolving ``P`` (the composed preset) through the
candidate — so a composer the judged version breaks is refused naming its route, the lines a composer
route already had before the write do not refuse it while a line the write adds still does, and the
candidate never leaks past the re-check."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastmcp.tools import Tool
from tai42_contract.conversations import ApiConversationRoute, ConversationRoute
from tai42_contract.presets import PresetBody
from tai42_contract.tools import ComposedToolRef
from tai42_contract.versioning.errors import DocumentNotFoundError

from tai42_skeleton.app.conversations_facet import ConversationsFacet
from tai42_skeleton.conversations import target_validators as tv_module
from tai42_skeleton.conversations.target_validators import TargetBindValidatorRegistry
from tai42_skeleton.operations.errors import ConflictError
from tai42_skeleton.operations.presets import bound_route_gate
from tai42_skeleton.presets.store import PresetStoreView

pytestmark = pytest.mark.asyncio


def _composer_tool(name: str) -> Tool:
    """A preset-like transformed tool over a ``composer`` base, so owner resolution reaches ``composer``."""
    return Tool.from_tool(Tool.from_function(lambda x: x, name="composer"), name=name)


class _FakeVersionedStore:
    def __init__(self, bodies: dict[str, dict]) -> None:
        self._bodies = bodies

    async def get_active_body(self, kind: str, name: str) -> dict:
        if name not in self._bodies:
            raise DocumentNotFoundError(kind, name)
        return self._bodies[name]


class _FakePresets:
    def __init__(self, bodies: dict[str, PresetBody]) -> None:
        self._bodies = bodies
        self.store = PresetStoreView(_FakeVersionedStore({n: b.model_dump() for n, b in bodies.items()}))  # pyright: ignore[reportArgumentType]

    async def list_active_bodies(self) -> dict[str, PresetBody]:
        return dict(self._bodies)


class _FakeAgents:
    def all_agents(self) -> dict[str, Any]:
        return {}


class _FakePresetManager:
    def is_registered(self, name: str) -> bool:
        # No preset is a live tool here: every body settles on its own base.
        return False


class _FakeTools:
    def __init__(self, tools: dict[str, Tool]) -> None:
        self._tools = tools

    async def get_tool(self, key: str) -> Tool:
        from tai42_skeleton.tools.binding import UnknownToolError

        tool = self._tools.get(key)
        if tool is None:
            raise UnknownToolError(key)
        return tool

    def tool_refs_extractor(self, base_tool: str) -> Callable[[dict[str, Any]], list[ComposedToolRef]] | None:
        # The ``composer`` base declares the tools its ``tool_names`` config composes.
        if base_tool != "composer":
            return None
        return lambda kwargs: [ComposedToolRef(name=name) for name in kwargs.get("tool_names", [])]

    def template_refs_extractor(self, base_tool: str) -> None:
        return None

    def base_of(self, name: str) -> str:
        # No extension branches are bound in this fake: every name is its own base.
        return name


class _FakeManager:
    def __init__(self, routes: dict[str, ConversationRoute]) -> None:
        self._routes = routes

    async def list_routes(self) -> tuple[dict[str, ConversationRoute], int]:
        return dict(self._routes), 0


class _FakeApp:
    def __init__(self, bodies: dict[str, PresetBody], tools: dict[str, Tool]) -> None:
        self.presets = _FakePresets(bodies)
        self.agents = _FakeAgents()
        self.preset_manager = _FakePresetManager()
        self.tools = _FakeTools(tools)
        self._target_validator_registry = TargetBindValidatorRegistry()
        self.conversations = ConversationsFacet(self)  # pyright: ignore[reportArgumentType]

    def target_validator(self, target_kind, target_name):
        return self._target_validator_registry.get(target_kind, target_name)


async def _owner_validator(route: Any, candidate: PresetBody | None) -> list[str]:
    """Models an owner validator: the target is broken, or it composes a preset that is broken.

    The composed preset is resolved through the preset store (candidate-aware), so a changed composed
    preset under re-check is seen as its not-yet-active version.
    """
    from tai42_skeleton.app import instance

    if candidate is None:
        return []
    lines = []
    if candidate.fixed_kwargs.get("broken"):
        lines.append(f"target {route.target_name!r} is broken")
    for composed in candidate.fixed_kwargs.get("tool_names", []):
        composed_body = await instance.app.presets.store.get_active_body(composed)
        if composed_body.fixed_kwargs.get("broken"):
            lines.append(f"target {route.target_name!r} composes broken preset {composed!r}")
    return lines


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


def _wire(
    monkeypatch,
    *,
    routes: dict[str, str],
    p_kwargs: dict[str, Any] | None = None,
    q_kwargs: dict[str, Any] | None = None,
) -> _FakeApp:
    """Wire P (a leaf preset) and Q (composes P via its tool_names, so Q is in P's used_by closure).

    ``routes`` maps each route name to the preset it is bound to.
    """
    bodies = {
        "p": PresetBody(base_tool="composer", description="d", fixed_kwargs=dict(p_kwargs or {})),
        "q": PresetBody(base_tool="composer", description="d", fixed_kwargs={"tool_names": ["p"], **(q_kwargs or {})}),
    }
    tools = {"p": _composer_tool("p"), "q": _composer_tool("q")}
    app = _FakeApp(bodies, tools)
    app.conversations.register_target_validator("tool", "composer", _owner_validator)

    from tai42_skeleton.app import instance

    monkeypatch.setattr(instance, "app", app, raising=False)
    # The write-chain helper lists routes through the conversations module's manager.
    import tai42_skeleton.conversations as conv_pkg

    bound: dict[str, ConversationRoute] = {
        route_name: _route(route_name, target) for route_name, target in routes.items()
    }
    monkeypatch.setattr(conv_pkg, "get_conversations_manager", lambda: _FakeManager(bound))
    # The candidate-body read is gated on the skeleton component store being configured (a save runs
    # with it configured); model that here so the composing route's own body resolves from the store.
    monkeypatch.setattr(tv_module, "component_store_configured", lambda *_: True)
    return app


@pytest.fixture
def wired_composing(monkeypatch):
    return _wire(monkeypatch, routes={"rq": "q"})


@pytest.fixture
def wired_composer_already_broken(monkeypatch):
    # Q cannot bind its route before any save of P, for a cause P has no part in.
    return _wire(monkeypatch, routes={"rq": "q"}, q_kwargs={"broken": True})


@pytest.fixture
def bind_check_calls(monkeypatch) -> list[str]:
    """Record the route name of every bind check the re-check runs."""
    calls: list[str] = []
    real = bound_route_gate.target_bind_refusal_lines

    async def _counting(create: Any, candidate: PresetBody | None) -> list[str]:
        calls.append(create.route_name)
        return await real(create, candidate)

    monkeypatch.setattr(bound_route_gate, "target_bind_refusal_lines", _counting)
    return calls


async def test_a_save_breaking_a_composing_route_is_refused_naming_it(wired_composing):
    from tai42_skeleton.operations.presets.bound_route_gate import SaveWrite, assert_bound_routes_still_bind

    broken_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"broken": True})
    with pytest.raises(ConflictError, match=r"'rq'.*composes broken preset 'p'"):
        await assert_bound_routes_still_bind("p", broken_p, SaveWrite())


async def test_a_save_keeping_a_composing_route_valid_passes(wired_composing):
    from tai42_skeleton.operations.presets.bound_route_gate import SaveWrite, assert_bound_routes_still_bind

    good_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"note": "fine"})
    await assert_bound_routes_still_bind("p", good_p, SaveWrite())


async def test_the_candidate_does_not_leak_after_the_recheck(wired_composing):
    from tai42_skeleton.app import instance
    from tai42_skeleton.operations.presets.bound_route_gate import SaveWrite, assert_bound_routes_still_bind

    broken_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"broken": True})
    with pytest.raises(ConflictError):
        await assert_bound_routes_still_bind("p", broken_p, SaveWrite())

    # A normal read after the re-check returns the COMMITTED body, not the candidate.
    committed = await instance.app.presets.store.get_active_body("p")
    assert committed.fixed_kwargs == {}


async def test_the_lines_a_composer_route_already_had_refuse_neither_a_save_nor_a_rollback(
    wired_composer_already_broken, caplog
):
    from tai42_skeleton.operations.presets.bound_route_gate import (
        RollbackWrite,
        SaveWrite,
        assert_bound_routes_still_bind,
    )

    saved_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"note": "v2"})
    rolled_back_to = PresetBody(base_tool="composer", description="d", fixed_kwargs={"note": "v1"})
    with caplog.at_level("WARNING", logger="tai42_skeleton.operations.presets.bound_route_gate"):
        await assert_bound_routes_still_bind("p", saved_p, SaveWrite())
        await assert_bound_routes_still_bind("p", rolled_back_to, RollbackWrite(version=1))

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    for message in warnings:
        assert "'rq'" in message
        assert "'q'" in message
        assert "target 'q' is broken" in message


async def test_a_save_adding_a_line_to_a_composer_that_could_not_bind_is_refused_for_that_line(
    wired_composer_already_broken,
):
    from tai42_skeleton.operations.presets.bound_route_gate import SaveWrite, assert_bound_routes_still_bind

    broken_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"broken": True})
    with pytest.raises(ConflictError) as refused:
        await assert_bound_routes_still_bind("p", broken_p, SaveWrite())
    assert (
        "route 'rq', bound to preset 'q': the new version adds a reason preset 'q' cannot bind "
        "(target 'q' composes broken preset 'p')"
    ) in str(refused.value)
    assert "would no longer bind" not in str(refused.value)
    assert "target 'q' is broken" not in str(refused.value)


async def test_a_route_bound_to_the_saved_preset_is_refused_even_when_it_could_not_bind_before(monkeypatch):
    from tai42_skeleton.operations.presets.bound_route_gate import SaveWrite, assert_bound_routes_still_bind

    _wire(monkeypatch, routes={"rp": "p"}, p_kwargs={"broken": True})
    still_broken_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"broken": True})
    with pytest.raises(ConflictError, match="target 'p' is broken"):
        await assert_bound_routes_still_bind("p", still_broken_p, SaveWrite())


async def test_the_refusal_names_the_preset_each_route_is_bound_to_and_what_is_owed(monkeypatch):
    from tai42_skeleton.operations.presets.bound_route_gate import SaveWrite, assert_bound_routes_still_bind

    _wire(monkeypatch, routes={"rp": "p", "rq": "q"})
    broken_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"broken": True})
    with pytest.raises(ConflictError) as refused:
        await assert_bound_routes_still_bind("p", broken_p, SaveWrite())
    assert str(refused.value) == (
        "preset 'p' cannot be saved — the new version would leave conversation route(s) unable to bind: "
        "route 'rp', bound to preset 'p': the new version cannot bind it (target 'p' is broken); "
        "route 'rq', bound to preset 'q': preset 'q' would no longer bind with the new version "
        "(target 'q' composes broken preset 'p')"
    )


async def test_a_save_that_breaks_nothing_runs_one_bind_check_per_route(monkeypatch, bind_check_calls):
    from tai42_skeleton.operations.presets.bound_route_gate import SaveWrite, assert_bound_routes_still_bind

    _wire(monkeypatch, routes={"rp": "p", "rq": "q"})
    good_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"note": "fine"})
    await assert_bound_routes_still_bind("p", good_p, SaveWrite())
    assert bind_check_calls == ["rp", "rq"]


async def test_a_failing_composer_route_costs_one_more_bind_check(monkeypatch, bind_check_calls):
    from tai42_skeleton.operations.presets.bound_route_gate import SaveWrite, assert_bound_routes_still_bind

    _wire(monkeypatch, routes={"rp": "p", "rq": "q"})
    broken_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"broken": True})
    with pytest.raises(ConflictError):
        await assert_bound_routes_still_bind("p", broken_p, SaveWrite())
    # The direct route is judged once; the failing composer route once with the candidate, once without.
    assert bind_check_calls == ["rp", "rq", "rq"]
