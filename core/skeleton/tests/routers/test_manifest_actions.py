"""The new manifest doors' authorization action-class, pinned per route.

The retighten + combined-secret surface pin their action class here rather than
inherit the method-derived read/write: ``GET /api/manifest/preserved`` is a plain
``read`` (markers intact, no resolved secret), and ``POST /api/mcp-config/secret-env``
mutates deployment config (writes a secret env value + a manifest marker) so it is the
admin-only ``fenced`` mutation. This test catches an accidental class flip that opened
the secret-env write onto the grantable surface or fenced the preserved read shut.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app.route_registry import load_all_routes

_EXPECTED_ACTIONS: dict[tuple[str, str], str] = {
    ("GET", "/api/manifest/preserved"): "read",
    ("POST", "/api/mcp-config/secret-env"): "fenced",
}


def _actions() -> dict[tuple[str, str], str]:
    return {(method, meta.path): meta.action for meta in load_all_routes() for method in meta.methods}


@pytest.mark.parametrize(("route", "expected"), sorted(_EXPECTED_ACTIONS.items()))
def test_manifest_door_action_class_is_pinned(route: tuple[str, str], expected: str) -> None:
    actions = _actions()
    assert route in actions, f"manifest route {route} is not registered"
    assert actions[route] == expected, f"{route} declares action={actions[route]!r}, expected {expected!r}"


def test_new_manifest_doors_are_registered_and_authed() -> None:
    routes = {(method, meta.path): meta for meta in load_all_routes() for method in meta.methods}
    for route in _EXPECTED_ACTIONS:
        assert route in routes, f"manifest route {route} is not registered"
        assert routes[route].authed, f"manifest route {route} must be authed"
