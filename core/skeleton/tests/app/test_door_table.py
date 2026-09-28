"""The shared route door table both app-level middlewares build on: the projection-driven
compile + specificity ordering (:func:`build_door_table`) and the registry-version memo
(:class:`DoorTable`)."""

from __future__ import annotations

from types import SimpleNamespace

from tai42_skeleton.app.route_registry import RouteMetadata, build_door_table
from tai42_skeleton.app.route_registry.door_table import DoorTable


def _meta(path: str, methods: list[str], *, max_body_bytes: int | None = None) -> RouteMetadata:
    """One registry entry with only the fields the door table reads carrying meaning."""
    return RouteMetadata(
        path=path,
        methods=tuple(sorted(m.upper() for m in methods)),
        name=path,
        summary="s",
        description="",
        tags=("t",),
        authed=False,
        request_model=None,
        response_model=None,
        reload_gated=False,
        reads_body=False,
        error_statuses=(),
        success_status=200,
        additional_success_statuses=(),
        success_media_types={},
        action="read",
        max_body_bytes=max_body_bytes,
    )


def test_build_skips_routes_the_projection_rejects():
    # A projection returning None drops the route; only the two declaring a bound survive.
    routes = [
        _meta("/a", ["POST"], max_body_bytes=10),
        _meta("/b", ["POST"]),  # declares none -> skipped
        _meta("/c", ["POST"], max_body_bytes=20),
    ]
    table = build_door_table(routes, lambda meta: meta.max_body_bytes)
    assert [door.payload for door in table] == [10, 20]  # /b absent


def test_build_orders_most_specific_first_and_folds_head_into_get():
    routes = [
        _meta("/{spa_path:path}", ["GET"]),  # catch-all, shortest static prefix
        _meta("/api/uploads", ["GET"]),  # longest static prefix
        _meta("/api/{name}", ["GET"]),  # mid
    ]
    table = build_door_table(routes, lambda meta: meta.path)
    assert [door.payload for door in table] == ["/api/uploads", "/api/{name}", "/{spa_path:path}"]
    # HEAD is answerable from a GET route, so the matcher carries it too.
    assert "HEAD" in table[0].methods


def test_door_for_matches_the_most_specific_and_respects_method():
    routes = [
        _meta("/{spa_path:path}", ["GET"]),
        _meta("/api/uploads", ["POST"]),
    ]
    table = DoorTable(
        lambda meta: meta.path,
        load_routes=lambda: routes,
        version_of=lambda: 1,
    )
    # POST /api/uploads matches the concrete door, not the catch-all (which is GET-only anyway).
    post = table.door_for("POST", "/api/uploads")
    assert post is not None
    assert post.payload == "/api/uploads"
    # GET /api/uploads has no matching door (uploads is POST-only), so the catch-all takes it.
    get = table.door_for("GET", "/api/uploads")
    assert get is not None
    assert get.payload == "/{spa_path:path}"
    # A method no door answers matches nothing.
    assert table.door_for("DELETE", "/api/uploads") is None


def test_the_table_is_stamped_with_the_pre_build_registry_version():
    # The version is read BEFORE the table compiles, so the memo can only ever UNDER-claim. A
    # door can be recorded DURING a build (``load_routes`` imports router modules, and an epoch
    # build records on its own thread): stamping the memo with the version read AFTER the build
    # would claim doors the table does not hold, and that door would stay uncovered.
    registry = SimpleNamespace(version=1)
    built_at: list[int] = []

    def _load() -> list[RouteMetadata]:
        built_at.append(registry.version)
        registry.version += 1  # a route recorded while this build is compiling
        return [_meta("/health", ["GET"])]

    table: DoorTable[str] = DoorTable(lambda meta: meta.path, load_routes=_load, version_of=lambda: registry.version)

    table.door_for("GET", "/health")
    assert table._memo is not None
    assert table._memo[0] == 1  # the PRE-build version, never the post-build 2
    # ... so the next lookup sees the memo lagging the registry and rebuilds against the
    # surface that grew mid-build.
    table.door_for("GET", "/health")
    assert built_at == [1, 2]


def test_reset_forces_a_recompile():
    registry = SimpleNamespace(version=1)
    builds: list[int] = []
    table: DoorTable[str] = DoorTable(
        lambda meta: meta.path,
        load_routes=lambda: (builds.append(1), [_meta("/health", ["GET"])])[1],
        version_of=lambda: registry.version,
    )
    table.door_for("GET", "/health")
    table.door_for("GET", "/health")  # same version -> memo hit, no rebuild
    assert len(builds) == 1
    table.reset()
    table.door_for("GET", "/health")  # reset dropped the memo -> rebuild
    assert len(builds) == 2
