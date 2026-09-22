"""``RawPathRoute`` matches on the raw request path so a percent-encoded slash keeps a path
parameter to one segment, and decodes each matched parameter once.

The single-record state doors register their ``{key}`` this way because a subject key
legitimately carries ``/`` (a thread key is ``bridge:{route}:{quote(principal)}/{id}``): on
the ordinary once-decoded path the ASGI server has already turned ``%2F`` into ``/`` and
split the key, 404-ing the record doors and mis-routing a key whose tail is a sub-action
name. These pin the matcher directly (no server), plus a registration check that every
record door is a ``RawPathRoute``.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.routing import Match

from tai42_skeleton.app.raw_path_route import RawPathRoute, SpaFallbackRoute

_RECORD = "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"
_SPA = "/{spa_path:path}"


async def _endpoint(request: Any) -> Any:  # pragma: no cover - never invoked; only matched
    raise AssertionError("endpoint should not run in a match test")


def _scope(raw: str, *, method: str = "GET", root_path: str = "") -> dict[str, Any]:
    # ``path`` is the once-decoded form the ASGI server produces (``%2F`` -> ``/``); the
    # matcher must ignore it in favor of ``raw_path``.
    return {
        "type": "http",
        "method": method,
        "path": raw.replace("%2F", "/"),
        "raw_path": raw.encode("ascii"),
        "root_path": root_path,
    }


def _match(route: RawPathRoute, raw: str, *, method: str = "GET", root_path: str = "") -> tuple[Match, Any]:
    match, child = route.matches(_scope(raw, method=method, root_path=root_path))
    return match, child.get("path_params", {})


def test_encoded_slash_key_is_one_decoded_segment() -> None:
    route = RawPathRoute(_RECORD, endpoint=_endpoint, methods=["GET"])
    match, params = _match(route, "/api/states/s/records/agent/relay/thread/bridge:r:p%2Fext")
    assert match is Match.FULL
    assert params["key"] == "bridge:r:p/ext"
    assert params["kind"] == "thread"


def test_key_whose_tail_is_a_subaction_matches_the_record_door() -> None:
    record = RawPathRoute(_RECORD, endpoint=_endpoint, methods=["GET"])
    writes = RawPathRoute(f"{_RECORD}/writes", endpoint=_endpoint, methods=["GET"])
    raw = "/api/states/s/records/agent/relay/thread/bridge:r:p%2Fwrites"
    record_match, params = _match(record, raw)
    writes_match, _ = _match(writes, raw)
    assert record_match is Match.FULL
    assert params["key"] == "bridge:r:p/writes"
    assert writes_match is Match.NONE


def test_writes_subaction_of_a_slashed_key() -> None:
    writes = RawPathRoute(f"{_RECORD}/writes", endpoint=_endpoint, methods=["GET"])
    match, params = _match(writes, "/api/states/s/records/agent/relay/thread/bridge:r:p%2Fext/writes")
    assert match is Match.FULL
    assert params["key"] == "bridge:r:p/ext"


def test_double_encoded_escape_is_decoded_once() -> None:
    # ``%252F`` decodes once to the literal text ``%2F`` — the same single decode an
    # ordinary segment gets from the ASGI server, never a second pass to ``/``.
    route = RawPathRoute(_RECORD, endpoint=_endpoint, methods=["GET"])
    _, params = _match(route, "/api/states/s/records/agent/relay/thread/a%252Fb")
    assert params["key"] == "a%2Fb"


def test_root_path_is_stripped_from_the_raw_path() -> None:
    route = RawPathRoute(_RECORD, endpoint=_endpoint, methods=["GET"])
    match, params = _match(route, "/mnt/api/states/s/records/agent/relay/thread/a%2Fb", root_path="/mnt")
    assert match is Match.FULL
    assert params["key"] == "a/b"


def test_method_mismatch_is_partial() -> None:
    route = RawPathRoute(_RECORD, endpoint=_endpoint, methods=["GET"])
    match, _ = _match(route, "/api/states/s/records/agent/relay/thread/a%2Fb", method="POST")
    assert match is Match.PARTIAL


def test_missing_raw_path_falls_back_to_the_decoded_match() -> None:
    # With no raw target (a non-uvicorn ASGI server), the once-decoded path is all there
    # is; a slashed key still splits there, so the record template does not match.
    route = RawPathRoute(_RECORD, endpoint=_endpoint, methods=["GET"])
    scope = _scope("/api/states/s/records/agent/relay/thread/a%2Fb")
    del scope["raw_path"]
    match, _ = route.matches(scope)
    assert match is Match.NONE


def test_a_non_ascii_raw_target_matches_no_raw_path_door() -> None:
    route = RawPathRoute(_RECORD, endpoint=_endpoint, methods=["GET"])
    scope = _scope("/api/states/s/records/agent/a/thread/k")
    scope["raw_path"] = "/api/states/s/records/agent/a/thread/caf\u00e9".encode()
    match, child = route.matches(scope)
    assert match is Match.NONE
    assert child == {}


def test_record_doors_are_marked_raw_path_matched() -> None:
    # ``use_raw_path_key`` marks the record-door metadata raw-path-matched at the SAME seam
    # it upgrades the served routes to ``RawPathRoute``, so this deterministic registry
    # assertion proves the whole family is raw-path-matched (the property authz reads).
    from tai42_skeleton.app.route_registry import load_all_routes, route_registry

    load_all_routes()  # imports the router universe, so states registered + the family marked
    record = [m for m in route_registry.routes() if m.path.startswith("/api/states/{name}/records/{target_kind}")]
    assert len(record) == 9
    assert all(m.raw_path_matched for m in record)


def test_use_raw_path_key_upgrades_the_served_routes_to_raw_path_routes() -> None:
    # The served-table counterpart: registering the record doors into an app and applying
    # ``use_raw_path_key`` upgrades every one of them to a ``RawPathRoute`` in place.
    from starlette.routing import Route

    _RECORD_PREFIX = "/api/states/{name}/records/{target_kind}"
    endpoints = [
        _RECORD,
        f"{_RECORD}/deltas",
        f"{_RECORD}/fold",
        f"{_RECORD}/writes",
    ]
    table: list[Route] = [Route(path, endpoint=_endpoint, methods=["GET"]) for path in endpoints]

    class _App:
        def __init__(self) -> None:
            self._fast_mcp = type("_FMcp", (), {"_additional_http_routes": table})()

    from tai42_skeleton.app.http import HttpSurface

    surface = HttpSurface(_App())  # type: ignore[arg-type]
    surface.use_raw_path_key(_RECORD)
    upgraded = [r for r in table if getattr(r, "path", "").startswith(_RECORD_PREFIX)]
    assert len(upgraded) == len(endpoints)
    assert all(isinstance(r, RawPathRoute) for r in upgraded)


# -- SpaFallbackRoute: the SPA catch-all held out of the /api and /mcp path spaces --------


def _spa_match(raw: str, *, method: str = "GET", root_path: str = "") -> Match:
    route = SpaFallbackRoute(_SPA, endpoint=_endpoint, methods=["GET"])
    match, _ = route.matches(_scope(raw, method=method, root_path=root_path))
    return match


@pytest.mark.parametrize("path", ["/api", "/api/tools", "/mcp", "/mcp/x", "/api/deep/link"])
def test_spa_fallback_never_matches_an_api_or_mcp_path(path) -> None:
    assert _spa_match(path) is Match.NONE
    # Even a wrong method stays NONE — never a PARTIAL that could 405 the shell surface, so an
    # unknown /api or /mcp path always falls to the router's native 404.
    assert _spa_match(path, method="POST") is Match.NONE


@pytest.mark.parametrize("path", ["/", "/agents/settings", "/apiary", "/mcpx", "/tools"])
def test_spa_fallback_matches_a_non_api_path(path) -> None:
    # Segment-aware, not a bare startswith: /apiary and /mcpx are genuine SPA paths.
    assert _spa_match(path) is Match.FULL


def test_spa_fallback_non_get_is_partial_on_a_non_api_path() -> None:
    # GET-only, so a non-API non-GET path is a PARTIAL -> the router's native 405.
    assert _spa_match("/agents/settings", method="POST") is Match.PARTIAL


def test_spa_fallback_strips_root_path_before_the_api_check() -> None:
    # A mounted root_path is stripped exactly as get_route_path does, so /mnt/api/x reads as /api.
    assert _spa_match("/mnt/api/x", root_path="/mnt") is Match.NONE
    assert _spa_match("/mnt/agents", root_path="/mnt") is Match.FULL


def test_use_spa_fallback_route_upgrades_the_catch_all_in_place() -> None:
    from starlette.routing import Route

    from tai42_skeleton.app.http import HttpSurface

    table: list[Route] = [
        Route("/api/known", endpoint=_endpoint, methods=["GET"]),
        Route(_SPA, endpoint=_endpoint, methods=["GET"]),
    ]

    class _App:
        def __init__(self) -> None:
            self._fast_mcp = type("_FMcp", (), {"_additional_http_routes": table})()

    surface = HttpSurface(_App())  # type: ignore[arg-type]
    surface.use_spa_fallback_route(_SPA)
    assert isinstance(table[1], SpaFallbackRoute)
    assert not isinstance(table[0], SpaFallbackRoute)  # only the catch-all is upgraded
    # Idempotent: a second call leaves the already-upgraded route as it is.
    already = table[1]
    surface.use_spa_fallback_route(_SPA)
    assert table[1] is already
