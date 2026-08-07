"""The settings-profile doors' authorization action-class, pinned per route.

The action-class is the authoritative fence a route sits behind (see
``app/route_registry.py``): ``read`` is grantable-by-tag, ``secret`` is the admin-only
bulk-secret READ fence, ``fenced`` is the admin-only MUTATION fence. The profile
doors PIN their class here rather than inherit the method-derived read/write — a
profile read/version/diff exposes REAL secret values (``secret``), and a save /
delete / rollback mutates deployment config (``fenced``). This test would catch an
accidental class flip that leaked a secret door onto the grantable surface.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app.route_registry import load_all_routes

# The pinned (method, path) → action-class for every profile door. The apply door is
# a separate later wave and is intentionally absent.
_EXPECTED_ACTIONS: dict[tuple[str, str], str] = {
    ("GET", "/api/config/profiles"): "read",
    ("GET", "/api/config/profiles/{name}"): "secret",
    ("PUT", "/api/config/profiles/{name}"): "fenced",
    ("DELETE", "/api/config/profiles/{name}"): "fenced",
    ("POST", "/api/config/profiles/{name}/diff"): "secret",
    ("GET", "/api/config/profiles/{name}/versions"): "read",
    ("GET", "/api/config/profiles/{name}/versions/{version}"): "secret",
    ("POST", "/api/config/profiles/{name}/rollback"): "fenced",
}


def _actions() -> dict[tuple[str, str], str]:
    return {(method, meta.path): meta.action for meta in load_all_routes() for method in meta.methods}


@pytest.mark.parametrize(("route", "expected"), sorted(_EXPECTED_ACTIONS.items()))
def test_profile_door_action_class_is_pinned(route: tuple[str, str], expected: str) -> None:
    actions = _actions()
    assert route in actions, f"profile route {route} is not registered"
    assert actions[route] == expected, f"{route} declares action={actions[route]!r}, expected {expected!r}"


def test_every_profile_door_is_registered_and_authed() -> None:
    # Every profile door must be present and AUTHED — a fenced/secret class is enforced
    # only in the authenticated path, so a public one would silently open it.
    routes = {(method, meta.path): meta for meta in load_all_routes() for method in meta.methods}
    for route in _EXPECTED_ACTIONS:
        assert route in routes, f"profile route {route} is not registered"
        assert routes[route].authed, f"profile route {route} must be authed"
