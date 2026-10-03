"""Standard 3, composing routes: a save of preset ``P`` re-checks every route bound to a preset that
COMPOSES ``P`` as a tool too, resolving ``P`` (the composed preset) through the candidate — so a
composer the new version breaks is refused naming its route, and the candidate never leaks past the
re-check."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp.tools import Tool
from tai42_contract.conversations import ConversationRoute
from tai42_contract.presets import PresetBody
from tai42_contract.versioning.errors import DocumentNotFoundError

from tai42_skeleton.app.conversations_facet import ConversationsFacet
from tai42_skeleton.conversations import target_validators as tv_module
from tai42_skeleton.conversations.target_validators import TargetBindValidatorRegistry
from tai42_skeleton.operations.errors import ConflictError
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


class _FakeTools:
    def __init__(self, tools: dict[str, Tool]) -> None:
        self._tools = tools

    async def get_tool(self, key: str) -> Tool:
        from tai42_skeleton.tools.binding import UnknownToolError

        tool = self._tools.get(key)
        if tool is None:
            raise UnknownToolError(key)
        return tool

    def tool_refs_extractor(self, base_tool: str) -> None:
        return None


class _FakeManager:
    def __init__(self, routes: dict[str, ConversationRoute]) -> None:
        self._routes = routes

    async def list_routes(self) -> tuple[dict[str, ConversationRoute], int]:
        return dict(self._routes), 0


class _FakeApp:
    def __init__(self, bodies: dict[str, PresetBody], tools: dict[str, Tool]) -> None:
        self.presets = _FakePresets(bodies)
        self.agents = _FakeAgents()
        self.tools = _FakeTools(tools)
        self._target_validator_registry = TargetBindValidatorRegistry()
        self.conversations = ConversationsFacet(self)  # pyright: ignore[reportArgumentType]


async def _owner_validator(route: Any, candidate: PresetBody | None) -> list[str]:
    """Models an owner validator: the target is broken, or it composes a preset that is broken.

    The composed preset is resolved through the preset store (candidate-aware), so a changed composed
    preset under re-check is seen as its unsaved version.
    """
    from tai42_skeleton.app import instance

    if candidate is None:
        return []
    if candidate.fixed_kwargs.get("broken"):
        return [f"target {route.target_name!r} is broken"]
    for composed in candidate.fixed_kwargs.get("tool_names", []):
        composed_body = await instance.app.presets.store.get_active_body(composed)
        if composed_body.fixed_kwargs.get("broken"):
            return [f"target {route.target_name!r} composes broken preset {composed!r}"]
    return []


@pytest.fixture
def wired_composing(monkeypatch):
    # P is a leaf preset; Q composes P via its tool_names, so Q is in P's used_by closure.
    bodies = {
        "p": PresetBody(base_tool="composer", description="d", fixed_kwargs={}),
        "q": PresetBody(base_tool="composer", description="d", fixed_kwargs={"tool_names": ["p"]}),
    }
    tools = {"p": _composer_tool("p"), "q": _composer_tool("q")}
    route_q = ConversationRoute(
        route_name="rq",
        door="api",
        target_kind="tool",
        target_name="q",
        execution_key="svc",
        callback_url="https://example.com/cb",
        execution_key_fingerprint="fp-1",
    )
    app = _FakeApp(bodies, tools)
    app.conversations.register_target_validator("tool", "composer", _owner_validator)

    from tai42_skeleton.app import instance

    monkeypatch.setattr(instance, "app", app, raising=False)
    # The write-chain helper lists routes through the conversations module's manager.
    import tai42_skeleton.conversations as conv_pkg

    monkeypatch.setattr(conv_pkg, "get_conversations_manager", lambda: _FakeManager({"rq": route_q}))
    # The candidate-body read is gated on the skeleton component store being configured (a save runs
    # with it configured); model that here so the composing route's own body resolves from the store.
    monkeypatch.setattr(tv_module, "component_store_configured", lambda *_: True)
    return app


async def test_a_save_breaking_a_composing_route_is_refused_naming_it(wired_composing):
    from tai42_skeleton.operations.presets.references import _assert_bound_routes_still_bind

    broken_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"broken": True})
    with pytest.raises(ConflictError, match=r"'rq'.*composes broken preset 'p'"):
        await _assert_bound_routes_still_bind("p", broken_p)


async def test_a_save_keeping_a_composing_route_valid_passes(wired_composing):
    from tai42_skeleton.operations.presets.references import _assert_bound_routes_still_bind

    good_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"note": "fine"})
    await _assert_bound_routes_still_bind("p", good_p)


async def test_the_candidate_does_not_leak_after_the_recheck(wired_composing):
    from tai42_skeleton.app import instance
    from tai42_skeleton.operations.presets.references import _assert_bound_routes_still_bind

    broken_p = PresetBody(base_tool="composer", description="d", fixed_kwargs={"broken": True})
    with pytest.raises(ConflictError):
        await _assert_bound_routes_still_bind("p", broken_p)

    # A normal read after the re-check returns the COMMITTED body, not the candidate.
    committed = await instance.app.presets.store.get_active_body("p")
    assert committed.fixed_kwargs == {}
