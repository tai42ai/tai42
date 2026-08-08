"""M20 F1 — the action-class surface, end-to-end (the runtime-reachable half of the
boot gate).

Every AUTHED route DECLARES its authorization action-class explicitly
(``read``/``write``/``fenced``/``secret``); allow-by-omission is a fail-open fence the
skeleton REFUSES at registration/boot (an authed route with no class, or a grantable
route whose class disagrees with its HTTP method, fails boot loudly). That boot-fail
INJECTION cannot be driven through this harness — the route surface is fixed importable
router modules, with no seam to register a synthetic bad route into a real boot — so it
is covered at the skeleton unit level (``tests/app/test_route_registry.py`` +
``tests/access_control/test_roles.py``, PLAN_2 Task 10). What the e2e leg CAN assert is
the runtime-reachable OUTCOME of that gate: a booted stack SERVES a fully-classified
action surface through ``GET /api/auth/routes`` — every gated route carries one of the
four valid classes, and the fence classes land on the routes the design fences.
"""

from __future__ import annotations

from typing import Any

from tai42_e2e.stack import TaiStack

_VALID_ACTIONS = {"read", "write", "fenced", "secret"}

# Representative routes and the action-class the design pins each to — the fence classes
# (``fenced`` mutations, ``secret`` bulk-reads including the two admin-only config reads)
# and the grantable read/write pair on one feature tag, so the surface is proven
# populated from the authoritative class, never divined by omission.
_EXPECTED: list[tuple[str, str, str]] = [
    ("POST", "/api/run-tool", "fenced"),
    ("POST", "/api/config/env", "fenced"),
    ("POST", "/api/config/reload", "fenced"),
    ("POST", "/api/manifest/replace", "fenced"),
    # The settings-profile doors: a list is a plain read; a body/version/diff read exposes
    # REAL secret values (the ``secret`` fence); a save / apply mutates the deployment (the
    # ``fenced`` mutation).
    ("GET", "/api/config/profiles", "read"),
    ("GET", "/api/config/profiles/{name}", "secret"),
    ("POST", "/api/config/profiles/{name}/diff", "secret"),
    ("PUT", "/api/config/profiles/{name}", "fenced"),
    ("POST", "/api/config/profiles/{name}/apply", "fenced"),
    ("GET", "/api/auth/roles", "secret"),
    ("GET", "/api/config/env", "secret"),
    ("GET", "/api/config/settings-schema", "secret"),
    ("GET", "/api/config/mode", "read"),
    ("GET", "/api/hooks", "read"),
    ("POST", "/api/hooks", "write"),
]


def _action_of(entries: list[dict[str, Any]], method: str, path: str) -> str | None:
    for entry in entries:
        if entry["path"] == path and method in entry.get("methods", []):
            return entry.get("action")
    raise AssertionError(f"route {method} {path} is absent from GET /api/auth/routes")


async def test_action_class_surface_is_fully_classified(auth_stack: TaiStack) -> None:
    admin = auth_stack.api()  # the seeded root "*" admin

    entries = await admin.get("/api/auth/routes")
    assert isinstance(entries, list), f"the routes surface must be a list: {entries!r}"
    assert entries, "the routes surface must be non-empty"

    # Any entry that carries an action carries a VALID class — never an unclassified
    # string leaking through (a ``None`` action is a non-registry-joined served path such
    # as the SPA catch-all, not a gated route).
    for entry in entries:
        action = entry.get("action")
        assert action is None or action in _VALID_ACTIONS, (
            f"route {entry['methods']} {entry['path']} carries an invalid action-class {action!r}"
        )

    # The fence classes land on the routes the design fences, and the grantable read/write
    # pair on the classes a level opens — the authoritative class is present, not omitted.
    for method, path, expected in _EXPECTED:
        assert _action_of(entries, method, path) == expected, (
            f"route {method} {path} must carry action={expected!r}; got {_action_of(entries, method, path)!r}"
        )
