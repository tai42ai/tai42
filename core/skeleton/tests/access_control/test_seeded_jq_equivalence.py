"""The seeded editor/viewer ceilings derived from route declarations grant the reach of the hand-written text.

A store seeded with the hand-written ceilings (the literal strings below, with each declared
plugin self-service route appended as an exact-path clause) keeps them: seeding is
create-only. Every registered control-plane route, on every method it serves, gets the same
verdict from that text and from the text derived from the routes' declarations, so a store
seeded either way grants the same reach — with one exception: the derived carve-in admits a
route's SERVED methods, and ``HEAD`` is served wherever ``GET`` is, while the hand-written
text pinned the read-only scopes listing to ``GET`` alone. ``HEAD`` on it (a ``GET`` without
the body) is the one verdict the derived text grants and the hand-written text does not.
"""

from __future__ import annotations

import json
import re

from tai42_kit.utils.data import run_jq_first

from tai42_skeleton.access_control.path_canon import canonicalize_path, under_prefix
from tai42_skeleton.access_control.role_gate import served_methods
from tai42_skeleton.access_control.roles import editor_jq, viewer_jq
from tai42_skeleton.app.route_registry import load_all_routes

_HAND_WRITTEN_EDITOR_CARVE = (
    '((.request.path | startswith("/api/auth")) | not) '
    'or (.request.path | startswith("/api/auth/api-keys")) '
    'or (.request.path == "/api/auth/tokens-payload") '
    'or (.request.path == "/api/auth/capabilities") '
    'or (.request.path == "/api/auth/me") '
    'or (.request.path == "/api/auth/claim-links") '
    'or ((.request.path == "/api/auth/scopes") and (.request.method == "GET")) '
    'or (.request.path == "/api/auth/logout")'
)

_HAND_WRITTEN_VIEWER_WRITE_CARVE = (
    '(.request.path | startswith("/api/auth/api-keys")) '
    'or (.request.path == "/api/auth/logout") '
    'or (.request.path == "/api/auth/claim-links")'
)

_READ_METHODS = '(.request.method | IN("GET","HEAD","OPTIONS"))'

_TEMPLATE_SEGMENT = re.compile(r"\{([^}:]+)(:path)?\}")


def _plugin_self_service_clauses() -> str:
    """The exact-path clause the hand-written text carried for each declared plugin self-service route."""
    paths = sorted({meta.path for meta in load_all_routes() if meta.self_service and meta.owner.kind == "plugin"})
    return "".join(f" or (.request.path == {json.dumps(path)})" for path in paths)


def _hand_written_editor() -> str:
    return f"({_HAND_WRITTEN_EDITOR_CARVE}{_plugin_self_service_clauses()})"


def _hand_written_viewer() -> str:
    plugin = _plugin_self_service_clauses()
    write_conjunct = f"({_HAND_WRITTEN_VIEWER_WRITE_CARVE}{plugin} or {_READ_METHODS})"
    ceiling = f"({_HAND_WRITTEN_EDITOR_CARVE}{plugin})"
    return f"({write_conjunct} and {ceiling})"


def _probe(template: str) -> str:
    """A concrete request path the template serves: each parameter filled with a sample segment."""
    return _TEMPLATE_SEGMENT.sub(lambda m: "a/b" if m.group(2) else "sample", template)


def _control_plane_pairs() -> list[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for meta in load_all_routes():
        canonical = canonicalize_path(meta.path)
        if meta.mounted or not under_prefix(canonical, "/api/auth"):
            continue
        pairs.update((_probe(canonical), method) for method in served_methods(meta))
    return sorted(pairs)


async def _verdict(jq: str, path: str, method: str) -> bool:
    return (await run_jq_first(jq, {"request": {"path": path, "method": method}})) is True


def test_the_comparison_covers_the_control_plane() -> None:
    pairs = _control_plane_pairs()
    # Non-vacuous: the own-key subtree, the read-only scopes listing and an admin-only area
    # are all compared.
    assert ("/api/auth/api-keys/sample", "PUT") in pairs
    assert ("/api/auth/scopes", "GET") in pairs
    assert ("/api/auth/scopes", "POST") in pairs
    assert ("/api/auth/roles", "GET") in pairs


async def test_derived_ceilings_match_the_hand_written_text() -> None:
    derived = {"editor": editor_jq(), "viewer": viewer_jq()}
    hand_written = {"editor": _hand_written_editor(), "viewer": _hand_written_viewer()}
    mismatches = [
        (role, method, path)
        for path, method in _control_plane_pairs()
        for role in ("editor", "viewer")
        if await _verdict(derived[role], path, method) is not await _verdict(hand_written[role], path, method)
    ]
    assert mismatches == [
        ("editor", "HEAD", "/api/auth/scopes"),
        ("viewer", "HEAD", "/api/auth/scopes"),
    ]
    for role in ("editor", "viewer"):
        assert await _verdict(derived[role], "/api/auth/scopes", "HEAD") is True
        assert await _verdict(derived[role], "/api/auth/scopes", "GET") is True
