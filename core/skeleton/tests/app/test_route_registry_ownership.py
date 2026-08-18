"""Route-registry ownership: the shape index, cross-owner collision raise,
same-owner replace, the concrete-path match API, and per-generation staging."""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import (
    CORE_OWNER,
    CrossOwnerRouteCollision,
    EpochRouteAuditError,
    RouteOwner,
    RouteRegistry,
)


async def _handler(request: Request) -> Response:
    """A plain handler."""
    return JSONResponse({"data": {}})


def _record(
    registry: RouteRegistry,
    path: str,
    methods: list[str],
    owner: RouteOwner,
    *,
    public: bool = False,
    authed: bool = True,
) -> None:
    if not authed:
        action = None
    elif any(method in {"POST", "PUT", "PATCH", "DELETE"} for method in methods):
        action = "write"
    else:
        action = "read"
    registry.record(
        path=path,
        methods=methods,
        name=None,
        handler=_handler,
        summary="s",
        tags=["t"],
        authed=authed,
        action=action,
        request_model=None,
        response_model=None,
        owner=owner,
        public=public,
    )


_PLUGIN_A = RouteOwner(kind="plugin", owner_ref="acme/one", item_name="web")
_PLUGIN_B = RouteOwner(kind="plugin", owner_ref="acme/two", item_name="relay")


def test_cross_owner_collision_raises() -> None:
    registry = RouteRegistry()
    # A concrete plugin route, then a DIFFERENT owner's template that overlaps it on
    # the shared method — the second registration raises, killing silent shadowing.
    _record(registry, "/api/e2e-epsilon/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    with pytest.raises(CrossOwnerRouteCollision):
        _record(registry, "/api/e2e-epsilon/{anything}", ["GET"], _PLUGIN_B, public=True, authed=False)


def test_same_owner_exact_re_record_replaces_without_raising() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    # A reload re-imports the same module and re-records the SAME (path, methods) —
    # a replace, never a collision.
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    assert registry.match("/api/acme/one/ping", "GET") is not None


def test_same_owner_disjoint_methods_do_not_collide() -> None:
    registry = RouteRegistry()
    # Same shape, disjoint methods, same owner — legal (two rows, one owner).
    _record(registry, "/api/acme/one/gate/{id}", ["GET"], _PLUGIN_A, authed=True)
    _record(registry, "/api/acme/one/gate/{id}", ["PUT"], _PLUGIN_A, authed=True)
    assert registry.match("/api/acme/one/gate/5", "GET") is not None
    assert registry.match("/api/acme/one/gate/5", "PUT") is not None


def test_cross_owner_disjoint_methods_do_not_collide() -> None:
    registry = RouteRegistry()
    # Same shape, DIFFERENT owners, but disjoint methods — no collision (they can
    # never both answer one concrete request+method).
    _record(registry, "/api/shared/{id}", ["GET"], _PLUGIN_A, authed=True)
    _record(registry, "/api/shared/{id}", ["POST"], _PLUGIN_B, authed=True)


def test_match_resolves_concrete_path_and_is_per_method() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/acme/one/chat/{id}", ["GET"], _PLUGIN_A, public=True, authed=False)
    _record(registry, "/api/acme/one/chat/{id}", ["POST"], _PLUGIN_A, authed=True)
    get_meta = registry.match("/api/acme/one/chat/42", "GET")
    assert get_meta is not None
    assert get_meta.public is True
    assert get_meta.owner == _PLUGIN_A
    post_meta = registry.match("/api/acme/one/chat/42", "POST")
    assert post_meta is not None
    assert post_meta.public is False
    # A method nobody declared resolves to nothing.
    assert registry.match("/api/acme/one/chat/42", "DELETE") is None
    # HEAD rides with GET.
    assert registry.match("/api/acme/one/chat/42", "HEAD") is not None


def test_match_treats_a_braced_request_segment_as_a_literal_not_a_template() -> None:
    registry = RouteRegistry()
    # A public literal door and, same owner, a protected single-segment template that
    # overlaps it on GET.
    _record(registry, "/api/e2e-epsilon/open", ["GET"], _PLUGIN_A, public=True, authed=False)
    _record(registry, "/api/e2e-epsilon/{slug}", ["GET"], _PLUGIN_A, authed=True)
    # A concrete request path literally spelling ``{x}`` is a LITERAL segment, not a
    # wildcard: it does not equal ``open`` and so must NOT resolve to the public door.
    # Pre-fix, the request path was parsed as a route SHAPE, so ``{x}`` became a
    # template that overlapped the literal ``open`` and, being the most specific match,
    # returned the PUBLIC entry — a spurious public resolution.
    braced = registry.match("/api/e2e-epsilon/{x}", "GET")
    assert braced is not None
    assert braced.public is False
    assert braced.path == "/api/e2e-epsilon/{slug}"
    # A normal concrete request still resolves the public literal door.
    literal = registry.match("/api/e2e-epsilon/open", "GET")
    assert literal is not None
    assert literal.public is True
    assert literal.path == "/api/e2e-epsilon/open"
    # And a request to any other concrete segment still matches the template route.
    templated = registry.match("/api/e2e-epsilon/other", "GET")
    assert templated is not None
    assert templated.path == "/api/e2e-epsilon/{slug}"


def test_non_api_and_mounted_routes_are_outside_the_shape_index() -> None:
    registry = RouteRegistry()
    _record(registry, "/health", ["GET"], CORE_OWNER, public=True, authed=False)
    registry.record_mounted(path="/mcp/{path:path}", methods=["GET", "POST"], name="m", summary="mount")
    assert registry.match("/health", "GET") is None
    assert registry.match("/mcp/x", "GET") is None


def test_match_prefers_the_more_specific_core_shape() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/plugins/{name}/studio/{path:path}", ["GET"], CORE_OWNER, authed=True)
    _record(registry, "/api/plugins/mine/studio/pinned", ["GET"], CORE_OWNER, authed=True)
    meta = registry.match("/api/plugins/mine/studio/pinned", "GET")
    assert meta is not None
    assert meta.path == "/api/plugins/mine/studio/pinned"


def test_shape_staging_commits_and_aborts() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/live/one", ["GET"], _PLUGIN_A, public=True, authed=False)

    # A build stages a fresh generation: the committed match surface is untouched
    # until commit, and the staged route is not yet matchable.
    registry.begin_shape_staging()
    registry.reset_shape_index()
    _record(registry, "/api/live/two", ["GET"], _PLUGIN_A, public=True, authed=False)
    assert registry.match("/api/live/one", "GET") is not None  # committed still live
    assert registry.match("/api/live/two", "GET") is None  # staged, not committed
    registry.commit_shape_staging()
    # After commit the staged generation IS the live one — the old route is gone.
    assert registry.match("/api/live/two", "GET") is not None
    assert registry.match("/api/live/one", "GET") is None


def test_shape_staging_abort_keeps_the_committed_surface() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/live/one", ["GET"], _PLUGIN_A, public=True, authed=False)
    registry.begin_shape_staging()
    registry.reset_shape_index()
    _record(registry, "/api/live/two", ["GET"], _PLUGIN_B, public=True, authed=False)
    registry.abort_shape_staging()
    # A failed build drops the staged generation; the live surface is exactly as before.
    assert registry.match("/api/live/one", "GET") is not None
    assert registry.match("/api/live/two", "GET") is None


def test_rollback_owner_removes_only_that_owners_routes_and_shapes() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    _record(registry, "/api/acme/two/relay", ["GET"], _PLUGIN_B, public=True, authed=False)
    registry.rollback_owner(_PLUGIN_A)
    # A gone from the match/collision shape index and the _routes dedup map; B intact.
    assert registry.match("/api/acme/one/ping", "GET") is None
    assert registry.match("/api/acme/two/relay", "GET") is not None
    paths = {meta.path for meta in registry.routes()}
    assert "/api/acme/one/ping" not in paths
    assert "/api/acme/two/relay" in paths
    # A's recorded route module is pruned too (cleanup symmetry): a rolled-back owner
    # registers nothing, so its reload extras drop with its routes. B's stay.
    assert registry.owner_route_modules(_PLUGIN_A) == frozenset()
    assert registry.owner_route_modules(_PLUGIN_B) == frozenset({_handler.__module__})


def test_rollback_owner_refuses_the_core_owner() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/thing", ["GET"], CORE_OWNER, authed=True)
    # Core routes share one owner and are not owner-isolable — a core rollback would
    # nuke the whole native surface, so it is refused rather than performed.
    with pytest.raises(ValueError, match="core"):
        registry.rollback_owner(CORE_OWNER)
    assert registry.match("/api/thing", "GET") is not None


def test_rollback_owner_targets_the_staged_generation_during_a_build() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/live/one", ["GET"], _PLUGIN_A, public=True, authed=False)
    registry.begin_shape_staging()
    registry.reset_shape_index()
    _record(registry, "/api/staged/a", ["GET"], _PLUGIN_A, public=True, authed=False)
    _record(registry, "/api/staged/b", ["GET"], _PLUGIN_B, public=True, authed=False)
    # A rollback during a build strips A from the STAGED target; the committed live
    # surface keeps serving until the swap.
    registry.rollback_owner(_PLUGIN_A)
    assert registry.match("/api/live/one", "GET") is not None
    registry.commit_shape_staging()
    assert registry.match("/api/staged/a", "GET") is None
    assert registry.match("/api/staged/b", "GET") is not None


def test_owner_route_modules_tracks_only_plugin_owners() -> None:
    registry = RouteRegistry()
    # A plugin route's registering module (where its handler is defined) is remembered
    # under the owner, so a reload can re-fire exactly that module.
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    assert registry.owner_route_modules(_PLUGIN_A) == frozenset({_handler.__module__})
    # A core route is not tracked — core stays leaf-only on reload.
    _record(registry, "/api/thing", ["GET"], CORE_OWNER, authed=True)
    assert registry.owner_route_modules(CORE_OWNER) == frozenset()
    # An owner that registered nothing yet is empty.
    assert registry.owner_route_modules(_PLUGIN_B) == frozenset()


def _stage_dropping(registry: RouteRegistry, keep: RouteOwner | None = None) -> None:
    """Open a staged generation that re-records only ``keep`` (or nothing) — the
    epoch-build state where a plugin's route-registering module failed to re-fire."""
    registry.begin_shape_staging()
    registry.reset_shape_index()
    if keep is not None:
        _record(registry, "/api/acme/one/ping", ["GET"], keep, public=True, authed=False)


def test_audit_raises_when_a_still_declared_plugin_lost_all_its_routes() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    # The rebuild staged a generation that dropped A's route entirely, but the new
    # manifest still declares A — a silent unmount the audit turns into a loud raise.
    _stage_dropping(registry, keep=None)
    with pytest.raises(EpochRouteAuditError, match="acme/one:web"):
        registry.audit_plugin_routes_preserved({_PLUGIN_A})


def test_audit_passes_when_the_plugin_was_dropped_from_the_manifest() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    # A staged generation without A's route is FINE when the new manifest no longer
    # declares A (A is not in ``expected_owners``) — its routes are legitimately gone.
    _stage_dropping(registry, keep=None)
    registry.audit_plugin_routes_preserved(set())


def test_audit_passes_when_the_plugin_re_registered() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    # A re-registered owner (even at a remapped path) keeps at least one staged route,
    # so the audit passes.
    _stage_dropping(registry, keep=_PLUGIN_A)
    registry.audit_plugin_routes_preserved({_PLUGIN_A})


def test_audit_is_a_no_op_outside_a_build() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    # No staged generation (a cold boot writes committed directly) — nothing to audit.
    registry.audit_plugin_routes_preserved({_PLUGIN_A})


def test_staged_collision_does_not_touch_the_committed_surface() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    registry.begin_shape_staging()
    registry.reset_shape_index()
    _record(registry, "/api/acme/one/ping", ["GET"], _PLUGIN_A, public=True, authed=False)
    with pytest.raises(CrossOwnerRouteCollision):
        _record(registry, "/api/acme/one/{x}", ["GET"], _PLUGIN_B, public=True, authed=False)
    registry.abort_shape_staging()
    survivor = registry.match("/api/acme/one/ping", "GET")
    assert survivor is not None
    assert survivor.owner == _PLUGIN_A
