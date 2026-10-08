"""The test helper that preserves every per-generation global across an epoch build."""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_kit.access_control import registry as identity_registry
from tai42_kit.registry import NamedFactoryRegistry, StagedGeneration

from tai42_skeleton.app import registry_staging
from tai42_skeleton.app.route_registry import RouteOwner, RouteRegistry

from .._staged_generations import (
    _snapshot,
    preserved_staged_generations,
    saved_objects,
    staged_participants,
    staged_primitives_of,
)


def test_participants_are_every_global_the_staging_phases_reference() -> None:
    participants = staged_participants()
    # The route registry's shape index is staged under its own method names, so it is
    # found by reference from the staging phases rather than by a shared method name.
    assert participants["route_registry"] is registry_staging.route_registry
    assert len(participants) >= 2


def test_a_registry_wrapping_a_staged_generation_yields_both_primitives() -> None:
    primitives = staged_primitives_of(identity_registry)
    assert any(isinstance(p, NamedFactoryRegistry) for p in primitives)
    assert any(isinstance(p, StagedGeneration) for p in primitives)


async def _handler(request: Request) -> Response:
    return JSONResponse({"data": {}})


def test_an_empty_committed_generation_is_restored_after_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = RouteRegistry()
    monkeypatch.setattr(registry_staging, "route_registry", registry)
    registry.record(
        path="/api/acme/one/ping",
        methods=["POST"],
        name=None,
        handler=_handler,
        summary="s",
        tags=["t"],
        authed=False,
        request_model=None,
        response_model=None,
        no_body_reason="test fixture: response body not under test",
        owner=RouteOwner(kind="plugin", owner_ref="acme/one", item_name="web"),
        public=True,
    )
    before = [(obj, _snapshot(obj)) for obj in saved_objects()]
    assert registry.match("/api/acme/one/ping", "POST") is not None

    with preserved_staged_generations():
        registry_staging.begin_staging_all()
        registry_staging.commit_staging_all()
        assert registry.match("/api/acme/one/ping", "POST") is None

    assert registry.match("/api/acme/one/ping", "POST") is not None
    after = [(obj, _snapshot(obj)) for obj in saved_objects()]
    assert after == before


def test_a_committed_identity_provider_is_restored_after_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    providers: NamedFactoryRegistry[object] = NamedFactoryRegistry("Gizmo")
    monkeypatch.setattr(identity_registry, "_PROVIDERS", providers)
    providers.register("acme", object)

    with preserved_staged_generations():
        registry_staging.begin_staging_all()
        registry_staging.commit_staging_all()
        assert providers.items() == []

    assert providers.items() == [("acme", object)]


def test_a_referenced_global_without_attribute_state_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_staging, "route_registry", (registry_staging.route_registry,))
    with pytest.raises(TypeError, match=r"registry_staging\.route_registry \(tuple\)"):
        staged_participants()


class _NoStagedState:
    def __init__(self) -> None:
        self.entries: dict[str, object] = {}

    def begin_shape_staging(self) -> None: ...

    def commit_shape_staging(self) -> None: ...

    def abort_shape_staging(self) -> None: ...


def test_a_referenced_global_reaching_no_staged_primitive_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_staging, "route_registry", _NoStagedState())
    with pytest.raises(TypeError, match=r"registry_staging\.route_registry \(_NoStagedState\) .* no staged-registry"):
        staged_participants()
