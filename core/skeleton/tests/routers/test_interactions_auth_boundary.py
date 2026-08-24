"""The interactions auth boundary, pinned with access control ENABLED.

A small ASGI app mounts the interactions routes behind the real three-middleware
stack (Authentication -> AuthContext -> ResourceGuard). The two callback doors and
the served-media door are public through the verifier's declared-public tier: they
are registered ``authed=False`` and the tier publics them straight from that
declaration, with NO per-deployment route row or path pattern (the pattern table is
empty). The pin needs no authenticated identity: it asserts those doors reach the
handlers with NO credentials, and that the ``authed=True`` ``/stream`` and
``/answer`` stay gated (rejected without auth).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tai42_skeleton.access_control import verifier as verifier_module
from tai42_skeleton.access_control.adapter import AuthAdapter
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.app.route_registry import CORE_OWNER, RouteAction, RouteRegistry
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.routers import interactions as router

from .._fakes.interactions_redis import FakeRedis as InteractionsFake
from ._auth_boundary import wire_store_from_route_strings


class _VersionRedis:
    """The verifier's plain-Redis policy-version read: ``get`` on the version key
    answers ``None`` (version 0). No route/pattern data flows through redis — the
    public grant comes from the registry declaration, not a stored row."""

    async def get(self, key):
        return None


def _record(
    registry: RouteRegistry, path: str, methods: list[str], *, public: bool, action: RouteAction | None = None
) -> None:
    registry.record(
        path=path,
        methods=methods,
        name=None,
        handler=router.callback,
        summary="s",
        tags=["t"],
        authed=not public,
        action=action,
        request_model=None,
        response_model=None,
        owner=CORE_OWNER,
        public=public,
    )


def _interactions_registry() -> RouteRegistry:
    """The interactions doors as production registers them: the callback and served-media
    doors ``authed=False`` (declared public), the studio surfaces ``authed=True`` (gated).
    The authed ``answer`` is recorded BEFORE the public ``callback`` so the equal-specificity
    same-owner overlap on ``/api/interactions/callback/answer`` resolves to the authed route."""
    registry = RouteRegistry()
    _record(registry, "/api/interactions/stream", ["GET"], public=False, action="read")
    _record(registry, "/api/interactions/pending", ["GET"], public=False, action="read")
    _record(registry, "/api/interactions/{interaction_id}/answer", ["POST"], public=False, action="write")
    _record(registry, "/api/interactions/media/{media_id}", ["GET"], public=True)
    _record(registry, "/api/interactions/callback/{ticket}", ["GET", "POST"], public=True)
    return registry


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # The callback door answers its uniform 404 when the interactions store is OFF
    # this boundary test exercises the ON door (a fake store stands in), so
    # configure it so the handler is reached.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def boundary_client(monkeypatch):
    # An EMPTY pattern table: the public grant provably flows from the authed=False
    # DECLARATION (the declared-public tier), not a pattern row.
    ac_settings = AccessControlSettings(path_patterns={})
    assert ac_settings.compiled_patterns == []

    @asynccontextmanager
    async def version_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield _VersionRedis()

    monkeypatch.setattr(verifier_module, "client_ctx", version_ctx)
    # The declared-public tier reads the route registry; seed it with the interactions
    # doors so the public grant flows from their registration. The authed doors resolve
    # to nothing (gated) against an empty policy store.
    monkeypatch.setattr(verifier_module, "route_registry", _interactions_registry())
    wire_store_from_route_strings(monkeypatch, {})

    # The callback handlers must not reach real Redis — a resolve of an unknown
    # ticket returning 404 is the "handler was reached" signal.
    interactions_fake = InteractionsFake()
    isettings = InteractionsSettings(public_base_url="https://cb.example")

    @asynccontextmanager
    async def interactions_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield interactions_fake

    monkeypatch.setattr(router, "client_ctx", interactions_ctx)
    monkeypatch.setattr(router, "interactions_settings", lambda: isettings)

    routes = [
        Route("/api/interactions/stream", router.stream, methods=["GET"]),
        Route("/api/interactions/pending", router.list_pending_interactions, methods=["GET"]),
        Route("/api/interactions/media/{media_id}", router.media, methods=["GET"]),
        Route("/api/interactions/{interaction_id}/answer", router.answer, methods=["POST"]),
        Route("/api/interactions/callback/{ticket}", router.callback, methods=["GET", "POST"]),
    ]
    app = Starlette(routes=routes, middleware=AuthAdapter(ac_settings).get_middleware())
    client = TestClient(app)
    # Expose the seam a verifier-boundary test seeds a bound question into.
    client.interactions_fake = interactions_fake
    client.interactions_settings = isettings
    return client


def test_callback_post_reachable_unauthenticated(boundary_client):
    resp = boundary_client.post("/api/interactions/callback/UNKNOWN", content=b"{}")
    # 404 from the handler (unknown ticket) proves the request reached it.
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_callback_get_reachable_unauthenticated(boundary_client):
    resp = boundary_client.get("/api/interactions/callback/UNKNOWN")
    assert resp.status_code == 404


def test_media_reachable_unauthenticated(boundary_client):
    # A well-formed but absent media id reaches the handler and returns its uniform 404,
    # proving the served-media door is public through the real middleware stack.
    resp = boundary_client.get("/api/interactions/media/" + "a" * 43)
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_stream_rejected_without_auth(boundary_client):
    resp = boundary_client.get("/api/interactions/stream")
    assert resp.status_code in (401, 403)


def test_answer_rejected_without_auth(boundary_client):
    resp = boundary_client.post("/api/interactions/i1/answer", json={"answer": "x"})
    assert resp.status_code in (401, 403)


def test_pending_rejected_without_auth(boundary_client):
    # The parked-interactions audit is an authed operator door, gated exactly like the
    # stream/answer surfaces — an unauthenticated request never reaches the handler.
    resp = boundary_client.get("/api/interactions/pending")
    assert resp.status_code in (401, 403)


class _PostOnlyVerifier:
    """A body-signature verifier: passes any body, signs the body only."""

    post_only = True

    async def verify(self, body, headers, config):
        return None

    def replay_defense(self, body, headers, config):
        from tai42_contract.webhooks import FreshnessWindow

        return FreshnessWindow()


async def _seed_bound_question(fake, settings, ticket: str, verifier: dict) -> str:
    from datetime import UTC, datetime, timedelta

    from tai42_contract.interactions import AnswerFormat, InteractionRequest

    from tai42_skeleton.interactions.store import InteractionStore

    store = InteractionStore(settings.key_prefix)
    now = datetime.now(UTC)
    request = InteractionRequest(
        interaction_id="i1",
        group_id="g1",
        question="Sign?",
        answer_format=AnswerFormat.EXTERNAL,
        format_payload={"url": "https://ext.example/resource", "verifier": verifier},
        reply_to=store.reply_key("i1"),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
    )
    await store.add(fake, request, idle_ttl=86400, ticket=ticket, ticket_ttl=60)
    return "i1"


def test_post_only_empty_body_query_answer_denied_unauthenticated(boundary_client):
    # The security fix holds through the real middleware stack with NO credentials:
    # a post_only verifier + empty body + ``?approved=true`` is denied (400), the
    # answer never injected — proving the public door still reaches the handler.
    import asyncio

    from tai42_contract.app import tai42_app

    from tai42_skeleton.app.instance import build_app

    app = build_app()
    tai42_app.bind(app)
    reg = app._webhook_verifier_registry
    reg.register("prov", _PostOnlyVerifier())
    try:
        asyncio.run(
            _seed_bound_question(
                boundary_client.interactions_fake,
                boundary_client.interactions_settings,
                "TKT",
                {"name": "prov", "config": {}},
            )
        )
        resp = boundary_client.post("/api/interactions/callback/TKT?approved=true", content=b"")
        assert resp.status_code == 400
        assert resp.json()["error"] == router._POST_ONLY_EMPTY_BODY_DENY
    finally:
        reg.reset()
