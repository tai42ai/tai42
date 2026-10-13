"""Routes declare their family's flood-control budget; the limiter reads the committed generation's declarations.

A neutral synthetic door family (``synthetic_family``) proves the seam: its routes declare a budget,
the limiter charges it, the boot/reload audit refuses a family whose public routes disagree, and a
reload that drops a route and moves the family's budget is charged at the new budget even though the
never-rolled-back dedup map still holds the dropped row.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient
from tai42_contract.app import DeclaredRouteMetadata, RateLimitBudget

from tai42_skeleton.app.route_registry import RouteRegistry
from tai42_skeleton.middleware import rate_limit
from tai42_skeleton.settings.rate_limit import RateLimitSettings

from .._fakes.interactions_redis import FakeRedis

_FAMILY = "synthetic_family"


async def _door(request: Request) -> Response:
    return PlainTextResponse("ok")


def _record(
    registry: RouteRegistry,
    path: str,
    budget: RateLimitBudget | None,
    *,
    family: str | None = _FAMILY,
    authed: bool = False,
) -> None:
    registry.record(
        path=path,
        methods=["GET"],
        name=None,
        handler=_door,
        summary="synthetic door",
        tags=["synthetic"],
        authed=authed,
        request_model=None,
        response_model=None,
        no_body_reason="synthetic door: plain text",
        public=not authed,
        action="read" if authed else None,
        declared=DeclaredRouteMetadata(
            reload_gated=False,
            reads_body=False,
            error_statuses=(),
            success_status=200,
            rate_limit_family=family,
            rate_limit_budget=budget,
        ),
    )


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[RouteRegistry]:
    """A fresh registry the limiter reads in place of the process one, from a cold memo."""
    fresh = RouteRegistry()
    monkeypatch.setattr(rate_limit, "route_registry", fresh)
    monkeypatch.setattr(rate_limit, "load_all_routes", fresh.routes)
    rate_limit._reset_door_table_cache()
    yield fresh
    rate_limit._reset_door_table_cache()


def _client(monkeypatch: pytest.MonkeyPatch, paths: list[str]) -> TestClient:
    monkeypatch.setenv("TAI_RATE_LIMIT_REDIS_URL", "redis://localhost:6379/0")
    settings = RateLimitSettings()
    monkeypatch.setattr(rate_limit, "rate_limit_settings", lambda: settings)
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: 100.0))
    fake = FakeRedis()

    @asynccontextmanager
    async def _ctx(cls, s=None, *, fresh=False, **kw):
        yield fake

    monkeypatch.setattr(rate_limit, "client_ctx", _ctx)
    app = Starlette(routes=[Route(path, _door, methods=["GET"]) for path in paths])
    return TestClient(rate_limit.RateLimitMiddleware(app), client=("peer", 50000))


# -- family_budgets ----------------------------------------------------------


def test_family_budgets_reads_each_public_familys_one_budget(registry: RouteRegistry) -> None:
    _record(registry, "/synthetic/a", RateLimitBudget(limit=3, burst=2))
    _record(registry, "/synthetic/b", RateLimitBudget(limit=3, burst=2))
    _record(registry, "/plain/door", None, family=None)
    budgets = rate_limit.family_budgets(registry.live_routes())
    assert budgets[_FAMILY] == RateLimitBudget(limit=3, burst=2)
    assert budgets["plain_door"] is None


def test_family_budgets_refuses_two_budgets_in_one_family(registry: RouteRegistry) -> None:
    _record(registry, "/synthetic/a", RateLimitBudget(limit=3, burst=2))
    _record(registry, "/synthetic/b", RateLimitBudget(limit=4, burst=2))
    with pytest.raises(rate_limit.RateLimitFamilyConflictError, match=r"synthetic_family.*/synthetic/a.*/synthetic/b"):
        rate_limit.family_budgets(registry.live_routes())


def test_family_budgets_refuses_a_route_that_forgets_to_declare(registry: RouteRegistry) -> None:
    _record(registry, "/synthetic/a", RateLimitBudget(limit=3, burst=2))
    _record(registry, "/synthetic/b", None)
    with pytest.raises(rate_limit.RateLimitFamilyConflictError, match="different budgets"):
        rate_limit.family_budgets(registry.live_routes())


def test_family_budgets_ignores_authed_routes_of_the_same_stem(registry: RouteRegistry) -> None:
    # The limiter never charges an authed door, so its declaration takes no part.
    _record(registry, "/synthetic/a", RateLimitBudget(limit=3, burst=2))
    _record(registry, "/synthetic/gate", None, authed=True)
    assert rate_limit.family_budgets(registry.live_routes())[_FAMILY] == RateLimitBudget(limit=3, burst=2)


def test_conflict_error_is_a_conflict_kind() -> None:
    from tai42_contract.errors import ErrorKind, error_kind

    assert error_kind(rate_limit.RateLimitFamilyConflictError("x")) is ErrorKind.CONFLICT


# -- the limiter charges the declared budget ----------------------------------


def test_a_declared_budget_is_charged(registry: RouteRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(registry, "/synthetic/a", RateLimitBudget(limit=3, burst=2))
    client = _client(monkeypatch, ["/synthetic/a"])
    responses = [client.get("/synthetic/a") for _ in range(3)]
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert int(responses[-1].headers["Retry-After"]) >= 1


def test_the_skeleton_doors_declare_sixty_and_ten() -> None:
    from tai42_skeleton.app.route_registry import load_all_routes

    routes = {meta.path: meta for meta in load_all_routes()}
    for path in (
        "/universal_webhook/{topic}",
        "/trigger/{token}",
        "/api/interactions/callback/{ticket}",
        "/api/interactions/callback/{ticket}/react",
    ):
        assert routes[path].rate_limit_budget == rate_limit.SKELETON_DOOR_BUDGET == RateLimitBudget(limit=60, burst=10)
    assert routes["/universal_webhook/{topic}"].error_statuses == (400, 401, 405, 413, 500)
    assert routes["/trigger/{token}"].error_statuses == (400, 403, 404, 413, 501)


# -- the boot and reload audit -----------------------------------------------


def test_the_audit_refuses_a_conflicting_registration(registry: RouteRegistry, monkeypatch) -> None:
    _record(registry, "/synthetic/a", RateLimitBudget(limit=3, burst=2))
    _record(registry, "/synthetic/b", RateLimitBudget(limit=9, burst=9))
    with pytest.raises(rate_limit.RateLimitFamilyConflictError):
        rate_limit.check_rate_limit_families()


def test_the_audit_reads_the_staged_generation_during_a_reload(registry: RouteRegistry) -> None:
    _record(registry, "/synthetic/a", RateLimitBudget(limit=3, burst=2))
    registry.begin_shape_staging()
    _record(registry, "/synthetic/a", RateLimitBudget(limit=3, burst=2))
    _record(registry, "/synthetic/b", RateLimitBudget(limit=9, burst=9))
    with pytest.raises(rate_limit.RateLimitFamilyConflictError):
        rate_limit.check_rate_limit_families()
    registry.abort_shape_staging()
    rate_limit.check_rate_limit_families()


def test_a_reload_that_drops_a_route_and_moves_the_budget_is_charged_at_the_new_one(
    registry: RouteRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(registry, "/synthetic/a", RateLimitBudget(limit=3, burst=2))
    _record(registry, "/synthetic/b", RateLimitBudget(limit=3, burst=2))
    client = _client(monkeypatch, ["/synthetic/a", "/synthetic/b"])
    assert client.get("/synthetic/a").status_code == 200

    registry.begin_shape_staging()
    _record(registry, "/synthetic/a", RateLimitBudget(limit=5, burst=4))
    rate_limit.check_rate_limit_families()
    before = registry.version
    registry.commit_shape_staging()
    assert registry.version == before + 1
    # The dedup map is never rolled back: the dropped route's old row is still in it.
    assert any(meta.path == "/synthetic/b" for meta in registry.routes())
    assert [meta.path for meta in registry.live_routes()] == ["/synthetic/a"]
    # Charged at the new burst of 4: one request already spent, three more pass, the fifth refuses.
    statuses = [client.get("/synthetic/a").status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]


# -- the registry's generation accessors --------------------------------------


def test_live_and_pending_routes_follow_the_build(registry: RouteRegistry) -> None:
    _record(registry, "/synthetic/a", None)
    assert [m.path for m in registry.live_routes()] == [m.path for m in registry.pending_routes()] == ["/synthetic/a"]
    registry.begin_shape_staging()
    _record(registry, "/synthetic/z", None)
    assert [m.path for m in registry.live_routes()] == ["/synthetic/a"]
    assert [m.path for m in registry.pending_routes()] == ["/synthetic/z"]
    registry.abort_shape_staging()
    assert [m.path for m in registry.live_routes()] == [m.path for m in registry.pending_routes()] == ["/synthetic/a"]


def test_commit_shape_staging_bumps_the_version(registry: RouteRegistry) -> None:
    registry.begin_shape_staging()
    before = registry.version
    registry.commit_shape_staging()
    assert registry.version == before + 1


def test_a_bare_fallback_route_cannot_declare_a_budget(registry: RouteRegistry) -> None:
    with pytest.raises(ValueError, match="bare rest-converter fallback route cannot declare rate_limit_budget"):
        _record(registry, "/{rest:path}", RateLimitBudget(limit=3, burst=2), family=None)


@pytest.mark.parametrize("budget", [RateLimitBudget(limit=0, burst=2), RateLimitBudget(limit=3, burst=-1)])
def test_a_non_positive_budget_is_refused(registry: RouteRegistry, budget: RateLimitBudget) -> None:
    with pytest.raises(ValueError, match="rate_limit_budget limit and burst must be positive"):
        _record(registry, "/synthetic/a", budget)
