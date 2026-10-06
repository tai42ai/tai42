"""Behavior of ``AccessControlVerifier``.

Covers token->identity translation and the route-resolution ladder (exact,
auto-normalized, explicit patterns, dynamic patterns). The route + dynamic-pattern
reads come from the PG store (the ``FakeAccessControlPg`` seeded per test), while the
policy-version read stays plain-Redis. The store
fetchers fail closed by RAISING: a backend error (or a corrupt stored pattern)
propagates rather than being cached as a degraded empty result, so the downstream
guard denies the request loudly instead of silently.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.identity import ApiKeyIdentityProvider, AuthIdentity, IdentityProvider

from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control import verifier as verifier_module
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.verifier import AccessControlVerifier
from tai42_skeleton.app.route_registry import RouteRegistry

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx, make_pg_ctx


async def _probe_handler(request: Request) -> Response:
    """A plain handler for registry fixtures."""
    return JSONResponse({"data": {}})


def _build_registry(*entries: tuple[str, list[str], bool]) -> RouteRegistry:
    """A registry carrying each ``(path, methods, public)`` entry as a handler route.

    ``public`` records the route ``authed=False`` (declared public); otherwise ``authed=True``.
    """
    registry = RouteRegistry()
    for path, methods, public in entries:
        registry.record(
            path=path,
            methods=methods,
            name=None,
            handler=_probe_handler,
            summary="s",
            tags=["t"],
            authed=not public,
            action=None if public else "read",
            request_model=None,
            response_model=None,
            no_body_reason="test fixture: response body not under test",
            public=public,
        )
    return registry


class _Provider(IdentityProvider):
    def __init__(self, identity: AuthIdentity | None) -> None:
        self._identity = identity

    async def validate_token(self, token: str) -> AuthIdentity | None:
        return self._identity


class _SpyProvider(IdentityProvider):
    """Records that it was consulted, and answers a fixed identity/None or raises."""

    def __init__(self, identity: AuthIdentity | None = None, *, raises: Exception | None = None) -> None:
        self._identity = identity
        self._raises = raises
        self.called = 0

    async def validate_token(self, token: str) -> AuthIdentity | None:
        self.called += 1
        if self._raises is not None:
            raise self._raises
        return self._identity


class _MintProvider(ApiKeyIdentityProvider):
    """An api-key (mint-capable) provider whose owner claim is NOT stripped."""

    def __init__(self, identity: AuthIdentity) -> None:
        self._identity = identity

    async def validate_token(self, token: str) -> AuthIdentity | None:
        return self._identity

    async def provision(
        self, user_id: str, description: str, *, owner_user_id: str | None = None
    ) -> str:  # pragma: no cover - unused
        return "sk-x"

    async def revoke(self, user_id: str) -> bool:  # pragma: no cover - unused
        return False

    async def update_description(self, user_id: str, description: str) -> bool:  # pragma: no cover - unused
        return False

    async def list_identities(self) -> list[tuple[str, str]]:  # pragma: no cover - unused
        return []


@pytest.fixture(autouse=True)
def _isolate_registered_reserved_memo():
    """Drop the module-level SPA-shell reserved-set memo around each test: it outlives any
    single verifier instance, so a warmed or faked surface would leak into the next test."""
    verifier_module.reset_registered_reserved_paths()
    try:
        yield
    finally:
        verifier_module.reset_registered_reserved_paths()


def _verifier(settings: AccessControlSettings | None = None, identity=None) -> AccessControlVerifier:
    return AccessControlVerifier(settings or AccessControlSettings(), providers=[_Provider(identity)])


def _wire(monkeypatch, pg: FakeAccessControlPg, redis: FakeRedis | None = None) -> None:
    """Route/pattern reads → the fake PG store; the version read → the fake Redis."""
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(verifier_module, "client_ctx", make_client_ctx(redis or FakeRedis()))


async def test_verify_token_returns_access_token_for_identity():
    v = _verifier(identity=AuthIdentity(user_id="u1", claims={"email": "a@b"}))
    token = await v.verify_token("raw")
    assert token is not None
    assert token.client_id == "u1"
    assert token.scopes == []
    assert token.claims == {"email": "a@b"}


async def test_verify_token_returns_none_when_no_identity():
    v = _verifier(identity=None)
    assert await v.verify_token("raw") is None


# -- provider chain ----------------------------------------------------------


async def test_chain_first_provider_wins_second_never_called():
    first = _SpyProvider(AuthIdentity(user_id="u1", claims={}))
    second = _SpyProvider(AuthIdentity(user_id="u2", claims={}))
    v = AccessControlVerifier(AccessControlSettings(), providers=[first, second])
    token = await v.verify_token("raw")
    assert token is not None
    assert token.client_id == "u1"
    assert first.called == 1
    assert second.called == 0  # short-circuit on first non-None


async def test_chain_falls_through_on_none():
    first = _SpyProvider(None)
    second = _SpyProvider(AuthIdentity(user_id="u2", claims={}))
    v = AccessControlVerifier(AccessControlSettings(), providers=[first, second])
    token = await v.verify_token("raw")
    assert token is not None
    assert token.client_id == "u2"
    assert first.called == 1
    assert second.called == 1


async def test_chain_all_none_returns_none():
    first = _SpyProvider(None)
    second = _SpyProvider(None)
    v = AccessControlVerifier(AccessControlSettings(), providers=[first, second])
    assert await v.verify_token("raw") is None


async def test_chain_error_propagates_and_never_reaches_next_provider():
    # A provider error propagates even when a later provider would match — an
    # unreachable primary must never silently shift auth onto a weaker provider.
    first = _SpyProvider(raises=RuntimeError("provider store down"))
    second = _SpyProvider(AuthIdentity(user_id="u2", claims={}))
    v = AccessControlVerifier(AccessControlSettings(), providers=[first, second])
    with pytest.raises(RuntimeError, match="provider store down"):
        await v.verify_token("raw")
    assert second.called == 0


async def test_owner_claim_stripped_from_non_mint_provider():
    # An external-issuer (non-ApiKeyIdentityProvider) that returns an owner claim has
    # it stripped centrally, so downstream attenuation never honors it.
    provider = _SpyProvider(AuthIdentity(user_id="u1", claims={OWNER_USER_ID_CLAIM: "victim", "email": "a@b"}))
    v = AccessControlVerifier(AccessControlSettings(), providers=[provider])
    token = await v.verify_token("raw")
    assert token is not None
    assert OWNER_USER_ID_CLAIM not in token.claims
    assert token.claims == {"email": "a@b"}


async def test_owner_claim_kept_for_mint_provider():
    # The mint path legitimately carries the owner claim: it is preserved.
    provider = _MintProvider(AuthIdentity(user_id="k1", claims={OWNER_USER_ID_CLAIM: "owner-1"}))
    v = AccessControlVerifier(AccessControlSettings(), providers=[provider])
    token = await v.verify_token("raw")
    assert token is not None
    assert token.claims[OWNER_USER_ID_CLAIM] == "owner-1"


async def test_mint_provider_identity_with_no_owner_claim_is_denied(caplog):
    # Every api key belongs to a principal: a mint-provider identity with NO owner claim is
    # an ownerless key record — an invariant breach. The verifier fails closed (the
    # credential reads as unresolved → the caller falls through to a deny), loudly logged,
    # never a silent ownerless admission.
    import logging

    provider = _MintProvider(AuthIdentity(user_id="k1", claims={"email": "a@b"}))
    v = AccessControlVerifier(AccessControlSettings(), providers=[provider])
    with caplog.at_level(logging.ERROR):
        assert await v.verify_token("raw") is None
    assert "no owner claim" in caplog.text


async def test_always_public_path_short_circuits_without_store_query(monkeypatch):
    # An always-public path returns exactly [public] and NEVER queries the store or
    # the version counter — proven by wiring both to raise on any access.
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.fault = ("SELECT", RuntimeError("store must not be queried"))
    redis = FakeRedis(raise_get=RuntimeError("version must not be read"))
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(verifier_module, "client_ctx", make_client_ctx(redis))
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/login/methods") == [settings.public_resource_id]
    assert await v.resolve_resource_ids("/api/login") == [settings.public_resource_id]
    assert pg.executed == []


async def test_resolve_exact_route_match(monkeypatch):
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/events", "events")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/events") == ["events"]


async def test_resolve_strips_trailing_slash(monkeypatch):
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/events", "events")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/events/") == ["events"]


async def test_resolve_auto_normalizes_uuid_and_digit(monkeypatch):
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/items/{id}", "items")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/items/42") == ["items"]


async def test_resolve_explicit_compiled_patterns(monkeypatch):
    settings = AccessControlSettings(path_patterns={r"^/files/.*$": "/files/template"})
    pg = FakeAccessControlPg()
    pg.add_route("/files/template", "files")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/files/anything/here") == ["files"]


async def test_explicit_patterns_use_fullmatch_not_prefix(monkeypatch):
    # ``fullmatch`` semantics: a pattern for /api/x/<digits> must not let a
    # longer, more-privileged path inherit the shorter path's resource id.
    settings = AccessControlSettings(path_patterns={r"/api/x/\d+": "/api/x/template"})
    pg = FakeAccessControlPg()
    pg.add_route("/api/x/template", "xroute")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/x/123") == ["xroute"]
    assert await v.resolve_resource_ids("/api/x/123/delete") == []


async def test_resolve_dynamic_patterns(monkeypatch):
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    # The dynamic pattern lives on the template url's row: /dyn/N matches the regex
    # and resolves through /dyn/template to its scope.
    pg.add_route("/dyn/template", "dynroute", pattern=r"^/dyn/\d+$")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/dyn/7") == ["dynroute"]


async def test_resolve_pattern_loops_skip_non_match_and_missing_route(monkeypatch):
    # One explicit pattern does not match the path; another matches but its template
    # has no stored route -> both loops add nothing.
    settings = AccessControlSettings(
        path_patterns={r"^/zzz$": "/t1", r"^/files/.*$": "/explicit-no-route"},
    )
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/files/x") == []


async def test_reserved_prefix_path_never_resolves_public_via_route_row(monkeypatch):
    # A reserved management path pinned public (directly on its route row) must NOT
    # resolve public: the verifier drops the marker so the control plane stays
    # authenticated regardless of the route table. An otherwise-unmapped reserved path
    # then resolves to nothing and is denied downstream.
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/api/auth/api-keys", settings.public_resource_id)
    # A path equal to the reserved prefix itself (not just a route beneath it) is also
    # dropped — the exact-match branch of the reserved check, not only the child branch.
    pg.add_route("/api/auth", settings.public_resource_id)
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/auth/api-keys") == []
    assert await v.resolve_resource_ids("/api/auth") == []


async def test_reserved_prefix_path_never_resolves_public_via_pattern(monkeypatch):
    # The pattern channel cannot open the control plane either: an unreserved url pinned
    # public with a dynamic pattern that fullmatches a reserved path resolves the marker
    # for that reserved path, but the verifier drops it — the reserved prefix is never
    # public no matter which write channel produced the mapping.
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/decoy", settings.public_resource_id, pattern=r"^/api/auth/.*$")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/auth/api-keys") == []
    # A non-reserved path the same pattern would not match is unaffected by the drop.
    assert await v.resolve_resource_ids("/decoy") == [settings.public_resource_id]


async def test_reserved_prefix_drop_leaves_protected_id(monkeypatch):
    # Dropping the marker never removes a protected id: a reserved path that also
    # resolves a real scope stays protected (the drop only strips the public marker).
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/api/auth/api-keys", settings.public_resource_id)
    pg.add_route("/protected-template", "admin", pattern=r"^/api/auth/api-keys$")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/auth/api-keys") == ["admin"]


async def test_public_exact_and_protected_pattern_both_resolve(monkeypatch):
    # Cross-tier deny-wins: a path that is BOTH a public exact match AND covered
    # by a protected dynamic pattern must resolve to BOTH ids. A short-circuit on
    # the exact tier would drop the protected id, and the guard would serve the
    # route as public-only with no auth.
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/mixed", "public")
    pg.add_route("/protected-template", "protected", pattern=r"^/mixed$")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert set(await v.resolve_resource_ids("/mixed")) == {"public", "protected"}


async def test_public_auto_normalized_and_protected_explicit_pattern_both_resolve(monkeypatch):
    # Same deny-wins guarantee across the auto-normalized tier and an explicit
    # pattern: an auto-normalized public match must not short-circuit past a
    # protected explicit-pattern match on the same path.
    settings = AccessControlSettings(path_patterns={r"^/items/\d+$": "/protected-template"})
    pg = FakeAccessControlPg()
    pg.add_route("/items/{id}", "public")
    pg.add_route("/protected-template", "protected")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert set(await v.resolve_resource_ids("/items/42")) == {"public", "protected"}


async def test_resolve_unknown_route_returns_empty(monkeypatch):
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/nope") == []


def test_normalize_auto_substitutions():
    v = _verifier()
    uuid = "/u/123e4567-e89b-12d3-a456-426614174000"
    assert v._normalize_auto(uuid) == "/u/{uuid}"
    assert v._normalize_auto("/n/55") == "/n/{id}"


def test_normalize_auto_uppercase_uuid():
    # UUID matching is case-insensitive: an uppercase UUID segment normalizes
    # to /{uuid} just like a lowercase one.
    v = _verifier()
    assert v._normalize_auto("/u/123E4567-E89B-12D3-A456-426614174000") == "/u/{uuid}"


async def test_dynamic_patterns_empty_when_no_hash(monkeypatch):
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v._raw_fetch_dynamic_patterns() == []


async def test_dynamic_patterns_raises_on_uncompilable_regex(monkeypatch):
    """A corrupt stored pattern is malformed config, not an empty result: it is
    surfaced loudly rather than silently dropped from the matched set."""
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/bad", "s1", pattern="(")
    pg.add_route("/ok", "s2", pattern=r"^/ok$")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    with pytest.raises(ValueError, match="malformed dynamic route pattern"):
        await v._raw_fetch_dynamic_patterns()


async def test_dynamic_patterns_raises_on_error_is_fail_closed(monkeypatch):
    """A dynamic-pattern fetch error fails closed by RAISING, so alru never caches
    a degraded empty result and the request is denied loudly downstream."""
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.fault = ("SELECT pattern, url FROM access_control_routes", RuntimeError("pg down"))
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    with pytest.raises(RuntimeError, match="pg down"):
        await v._raw_fetch_dynamic_patterns()


async def test_raw_fetch_route_hit(monkeypatch):
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/x", "routex")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v._raw_fetch_route("/x") == "routex"


async def test_raw_fetch_route_raises_on_error_is_fail_closed(monkeypatch):
    """A route-map fetch error fails closed by RAISING, so alru never caches a
    degraded ``None`` and the request is denied loudly rather than silently."""
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.fault = ("SELECT scope_id FROM access_control_routes", RuntimeError("pg down"))
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    with pytest.raises(RuntimeError, match="pg down"):
        await v._raw_fetch_route("/x")


async def test_route_repoint_visible_to_warm_cache_after_version_bump(monkeypatch):
    """A route re-point via management is visible to a second reader the instant
    the policy version is bumped, WITHOUT waiting out the cache ttl — the verifier
    route cache is version-aware, mirroring the policy cache."""
    from tai42_skeleton.access_control import management

    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    redis = FakeRedis(strings={})
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(verifier_module, "client_ctx", make_client_ctx(redis))
    monkeypatch.setattr(management, "client_ctx", make_client_ctx(redis))

    await management.add_url_to_scope("weak", "/admin")
    v = _verifier(settings)
    # Warm the route cache at the current version.
    assert await v.resolve_resource_ids("/admin") == ["weak"]

    # Operator locks the route down to a stronger scope (overwrites the mapping)
    # WITHOUT bumping the version: the warm cache still serves the old scope
    # (proves the cache is actually warm — a bounded fail-open without the fix).
    await management.add_url_to_scope("strong", "/admin")
    assert await v.resolve_resource_ids("/admin") == ["weak"]

    # Every scope route bumps the version → cross-worker cache miss, the re-point
    # is visible immediately without waiting out the ttl.
    await management.bump_policy_version()
    assert await v.resolve_resource_ids("/admin") == ["strong"]


async def test_dynamic_pattern_change_visible_after_version_bump(monkeypatch):
    """The dynamic-pattern cache is version-aware too: a pattern registered after
    the cache warmed is visible once the version is bumped, without the ttl."""
    from tai42_skeleton.access_control import management

    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    redis = FakeRedis(strings={})
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(verifier_module, "client_ctx", make_client_ctx(redis))
    monkeypatch.setattr(management, "client_ctx", make_client_ctx(redis))

    v = _verifier(settings)
    # Warm the dynamic-pattern cache while empty.
    assert await v.resolve_resource_ids("/dyn/7") == []

    # Register a dynamic pattern + its route WITHOUT bumping: the warm empty cache
    # still resolves nothing.
    await management.add_url_to_scope("dynroute", "/dyn/template", pattern=r"^/dyn/\d+$")
    assert await v.resolve_resource_ids("/dyn/7") == []

    # Bump the version → both the route and the dynamic-pattern caches miss and
    # re-read, so the new pattern-scoped route resolves.
    await management.bump_policy_version()
    assert await v.resolve_resource_ids("/dyn/7") == ["dynroute"]


# -- SPA-shell public fallback + H1 canonicalization -------------------------
#
# The last resolution tier: a GET to an UNMAPPED, non-/api, non-/mcp canonical path
# that is NOT a registered route resolves public so a deep-link refresh reaches the
# dataless SPA shell. Every classification runs on ONE canonical path form.


async def test_spa_fallback_unmapped_get_is_public(monkeypatch):
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/agents", method="GET") == [settings.public_resource_id]
    # Any inner Studio route, too.
    assert await v.resolve_resource_ids("/agents/123/detail", method="GET") == [settings.public_resource_id]


async def test_spa_fallback_only_fires_for_get(monkeypatch):
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    # A non-GET method never opens the shell (it would be a mutation on an unmapped path).
    assert await v.resolve_resource_ids("/agents", method="POST") == []
    # An unspecified method is fail-closed: the fallback never fires.
    assert await v.resolve_resource_ids("/agents", method=None) == []
    assert await v.resolve_resource_ids("/agents") == []


async def test_spa_fallback_skips_registered_operational_routes(monkeypatch):
    # The SPA-shell fallback never opens a REGISTERED route: a GET to a registered non-/api
    # route is in the DERIVED reserved set, so the shell tier skips it. Proven with a
    # NON-PUBLIC fake registry — /health, /ready are registered AUTHED, so the declared-public
    # tier does not fire either — and with no route row they resolve to nothing, which is only
    # possible if the shell tier refused to open a registered path.
    from types import SimpleNamespace

    fake_routes = [
        SimpleNamespace(path="/health", methods=("GET",), mounted=False),
        SimpleNamespace(path="/ready", methods=("GET",), mounted=False),
    ]
    monkeypatch.setattr(verifier_module, "load_all_routes", lambda: fake_routes)
    monkeypatch.setattr(
        verifier_module, "route_registry", _build_registry(("/health", ["GET"], False), ("/ready", ["GET"], False))
    )
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    for path in ("/health", "/ready"):
        assert await v.resolve_resource_ids(path, method="GET") == []


async def test_spa_fallback_skips_openapi_via_supplement(monkeypatch):
    # /openapi.json is not a live registered route; the supplement keeps the fallback
    # from serving the shell for the conventional OpenAPI document path.
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/openapi.json", method="GET") == []


async def test_spa_fallback_never_opens_api(monkeypatch):
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/anything", method="GET") == []


async def test_spa_fallback_deny_wins_over_explicit_protected_mapping(monkeypatch):
    # An operator who explicitly mapped /agents to a protected scope keeps it protected:
    # the fallback fires ONLY when the route table resolved nothing.
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/agents", "agents-scope")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/agents", method="GET") == ["agents-scope"]


async def test_spa_fallback_reserved_prefix_stays_gated(monkeypatch):
    # A reserved-prefix path (also under /api) never opens via the fallback.
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/auth/whatever", method="GET") == []


async def test_spa_fallback_master_switch_off(monkeypatch):
    settings = AccessControlSettings(spa_shell_public=False)
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/agents", method="GET") == []


async def test_spa_reserved_set_is_derived_not_static(monkeypatch):
    # A NEWLY registered non-/api GET route is automatically in the derived reserved set
    # (the fallback skips it) with NO settings change — pins the no-static-list property.
    from types import SimpleNamespace

    fake_routes = [
        SimpleNamespace(path="/newpage", methods=("GET",), mounted=False),
        SimpleNamespace(path="/api/agents", methods=("GET",), mounted=False),  # /api excluded
        SimpleNamespace(path="/webhook/{topic}", methods=("GET",), mounted=False),  # templated excluded
    ]
    monkeypatch.setattr(verifier_module, "load_all_routes", lambda: fake_routes)
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    # The freshly declared route is reserved (gated) with no static list edit.
    assert await v.resolve_resource_ids("/newpage", method="GET") == []
    # A genuinely unmapped, unregistered path still reaches the shell.
    assert await v.resolve_resource_ids("/otherpage", method="GET") == [settings.public_resource_id]


def test_mounted_transport_surfaces_are_not_in_the_derived_reserved_set(monkeypatch):
    # A mounted transport path is concrete, non-/api and GET — the shape the derivation
    # otherwise reserves — but the SPA shell never answers for it: its mount matches first
    # and gates it on the credential. It is no part of the shell's reserved set.
    from types import SimpleNamespace

    monkeypatch.setattr(
        verifier_module,
        "load_all_routes",
        lambda: [
            SimpleNamespace(path="/sse", methods=("GET",), mounted=True),
            SimpleNamespace(path="/ready", methods=("GET",), mounted=False),
        ],
    )
    assert verifier_module.registered_reserved_get_paths() == frozenset({"/ready"})


async def test_reload_added_route_not_served_anonymously_after_reset(monkeypatch):
    # The HTTP-edge verifier is built once and baked into the ASGI stack, so the same
    # instance answers across a reload: its reserved set must not stay frozen on the
    # pre-reload surface, or a reload-added route is served the anonymous SPA shell.
    from types import SimpleNamespace

    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)  # one persistent verifier across the reload

    # Pre-reload surface: /status is NOT a registered route, so a GET reaches the shell.
    monkeypatch.setattr(verifier_module, "load_all_routes", list)
    assert await v.resolve_resource_ids("/status", method="GET") == [settings.public_resource_id]

    # The reload re-imports a router that registers an authed GET /status.
    monkeypatch.setattr(
        verifier_module, "load_all_routes", lambda: [SimpleNamespace(path="/status", methods=("GET",), mounted=False)]
    )

    # Until the reset drops it the memo is stale, so /status still gets the anonymous shell.
    assert await v.resolve_resource_ids("/status", method="GET") == [settings.public_resource_id]

    # After the reload's drop the same verifier derives /status into the reserved set.
    verifier_module.reset_registered_reserved_paths()
    assert await v.resolve_resource_ids("/status", method="GET") == []


# -- H1 bypass corpus (MUST): one canonical form, segment-aware prefixes ------


async def test_h1_bypass_corpus(monkeypatch):
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    public = [settings.public_resource_id]

    # ``%61`` decodes once to ``a`` → /api/x, an unmapped /api path — gated (never shell).
    assert await v.resolve_resource_ids("/%61pi/x", method="GET") == []
    # Duplicate leading slash collapses to /api/x — gated.
    assert await v.resolve_resource_ids("//api/x", method="GET") == []
    # Dot-resolution first: /api/../agents is genuinely /agents — shell-public.
    assert await v.resolve_resource_ids("/api/../agents", method="GET") == public
    # …and /agents/../api/secret is genuinely /api/secret — gated.
    assert await v.resolve_resource_ids("/agents/../api/secret", method="GET") == []
    # An encoded slash (%2F) keeps the key one segment (canonical /api%2Fx), which the ASGI
    # router would decode to a real /api/ segment and route elsewhere. It resolves to no
    # raw-path-matched route, so it is denied fail-closed rather than served as the shell.
    assert await v.resolve_resource_ids("/api%2Fx", method="GET") == []
    # Case-sensitive: /API is not the lowercase /api mount, so it never reaches API
    # DATA; shell-or-deny is acceptable (here: the shell).
    assert await v.resolve_resource_ids("/API/x", method="GET") == public


async def test_h1_malformed_paths_denied(monkeypatch):
    # A NUL / control / backslash path is malformed → fail-closed deny, never the shell.
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    for bad in ("/agents\x00", "/agents\\admin", "/agents\x1f"):
        assert await v.resolve_resource_ids(bad, method="GET") == []


# -- Declared-public tier: registered probes and doors served public by their declaration --
#
# A request to a registered non-/api route DECLARED public (``authed=False``) resolves public
# straight from its registration — per served method, with NO always-public entry and NO route
# row — so a fresh access-control-on deployment serves /health, /ready without a 403.
# ``acknowledged_public_routes`` is a BOOT sign-off; the runtime no longer consults it. The
# declaration is authoritative above the route table, and the control plane can never enter it.


async def test_declared_public_probe_is_public(monkeypatch):
    # /health and /ready are registered non-/api public GET routes. With NO always-public
    # prefix covering them and NO route row, they resolve to exactly [public] by declaration.
    settings = AccessControlSettings()
    # Prove they are NOT covered by an always-public prefix — the declaration is what
    # resolves them to public, not an always-public short-circuit.
    assert not any(p in ("/health", "/ready") for p in settings.always_public_path_prefixes)
    monkeypatch.setattr(
        verifier_module, "route_registry", _build_registry(("/health", ["GET"], True), ("/ready", ["GET"], True))
    )
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    for path in ("/health", "/ready"):
        assert await v.resolve_resource_ids(path, method="GET") == [settings.public_resource_id]


async def test_declared_public_probe_canonicalizes_trailing_slash(monkeypatch):
    # The tier keys on the single canonical path: a trailing-slash probe (/health/) is
    # canonicalized to /health and still resolves public, so a proxy or client that appends
    # a slash does not fall through to a 403.
    settings = AccessControlSettings()
    monkeypatch.setattr(verifier_module, "route_registry", _build_registry(("/health", ["GET"], True)))
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/health/", method="GET") == [settings.public_resource_id]


async def test_declared_public_probe_serves_get_and_head_not_other_methods(monkeypatch):
    # GET and HEAD open a public probe (HEAD rides with GET by construction, so a HEAD
    # healthcheck is not denied); a method the route does not declare, or an unspecified
    # method, never does.
    settings = AccessControlSettings()
    monkeypatch.setattr(verifier_module, "route_registry", _build_registry(("/health", ["GET"], True)))
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/health", method="GET") == [settings.public_resource_id]
    assert await v.resolve_resource_ids("/health", method="HEAD") == [settings.public_resource_id]
    assert await v.resolve_resource_ids("/health", method="POST") == []
    assert await v.resolve_resource_ids("/health", method=None) == []
    assert await v.resolve_resource_ids("/health") == []


async def test_declared_public_wins_over_a_protected_route_row(monkeypatch):
    # The declaration is authoritative ABOVE the route table: a declared-public route a later
    # operator pinned to a protected scope still resolves public — a row no longer re-protects
    # a declared-public route of any path shape (the declaration short-circuits above the table).
    settings = AccessControlSettings()
    monkeypatch.setattr(verifier_module, "route_registry", _build_registry(("/health", ["GET"], True)))
    pg = FakeAccessControlPg()
    pg.add_route("/health", "ops-only")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/health", method="GET") == [settings.public_resource_id]


async def test_declared_public_grant_does_not_need_acknowledgment(monkeypatch):
    # Acknowledgment is a BOOT sign-off, not a runtime gate: a registered public GET resolves
    # public by its declaration even when the path is not in ``acknowledged_public_routes``.
    settings = AccessControlSettings()
    assert "/internal-probe" not in settings.acknowledged_public_routes
    monkeypatch.setattr(verifier_module, "route_registry", _build_registry(("/internal-probe", ["GET"], True)))
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/internal-probe", method="GET") == [settings.public_resource_id]


async def test_unregistered_path_is_not_public_even_if_acknowledged(monkeypatch):
    # An acknowledged path that is NOT a registered route does not gain public: ``match``
    # finds no route and, with the SPA-shell fallback OFF so nothing masks the result, an
    # acknowledged-but-unregistered /health resolves to nothing.
    monkeypatch.setattr(verifier_module, "route_registry", RouteRegistry())  # empty registry
    monkeypatch.setattr(verifier_module, "load_all_routes", list)
    settings = AccessControlSettings(spa_shell_public=False)
    assert "/health" in settings.acknowledged_public_routes
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/health", method="GET") == []


def test_acknowledged_public_routes_forbids_api_mcp_entries():
    # Invariant (b): the control plane / an /api or /mcp path can NEVER be acknowledged
    # public — settings construction rejects it, so no such entry can ever reach the boot audit.
    for bad in ("/api/secret", "/api/auth/keys", "/mcp/tool"):
        with pytest.raises(ValueError, match="acknowledged_public_routes"):
            AccessControlSettings(acknowledged_public_routes=("/health", bad))


async def test_declared_public_tier_reserved_prefix_never_public(monkeypatch):
    # Belt-and-suspenders runtime guard for invariant (b): even a registered public route
    # under a reserved prefix is dropped by the tier's ``_is_reserved_prefix`` guard. Mark a
    # NON-/api registered public path reserved — the reserved guard must still deny it.
    settings = AccessControlSettings(reserved_public_pin_prefixes=("/api/auth", "/control"))
    monkeypatch.setattr(verifier_module, "route_registry", _build_registry(("/control", ["GET"], True)))
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/control", method="GET") == []


# -- Always-public route patterns: the plugin studio-asset door --------------


async def test_plugin_studio_asset_is_public(monkeypatch):
    # The plugin studio-asset door (/api/plugins/{name}/studio/{path}, registered
    # authed=False) resolves public via the default always-public route pattern — the
    # acknowledged tier excludes /api, and no fixed prefix reaches /studio/ after {name}.
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    for path in (
        "/api/plugins/tai42_example_plugin/studio/main-abc123.css",
        "/api/plugins/x/studio/nested/asset.js",
    ):
        assert await v.resolve_resource_ids(path, method="GET") == [settings.public_resource_id]


async def test_plugin_list_not_public_and_bare_studio_publics_harmlessly(monkeypatch, bound_app):
    # The authed /api/plugins LIST is genuinely non-public: it carries no ``public``
    # declaration and matches no always-public pattern, so it is never granted the public
    # id. With no operator row it resolves to the universal scope ``["*"]`` via the
    # declared-protection tier — a registered authenticated surface a role-holder reaches,
    # a scoped key does not — NOT the public id.
    #
    # The bare /api/plugins/{name}/studio (no asset) now resolves ['public'] via the
    # owner-agnostic declared-public tier: ``route_registry.match`` reports the CORE
    # studio-asset route ``/api/plugins/{name}/studio/{path:path}`` (public=True) as the
    # owner of that concrete path, because the shape algebra lets the terminal
    # ``{path:path}`` swallow an EMPTY tail. This grant is HARMLESS: no handler serves the
    # bare path — Starlette requires the ``/`` before ``{path:path}`` so it never routes
    # ``/api/plugins/x/studio`` to the asset handler (it 404s), and the trailing-slash
    # form routes with ``path=""`` which the handler 404s (not in the integrity set, not a
    # file). A public grant on a path that serves no data is inert; the security invariant
    # "no non-public route serving data is reachable unauthenticated" is unaffected.
    settings = AccessControlSettings()
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/plugins", method="GET") == ["*"]
    assert await v.resolve_resource_ids("/api/plugins/x/studio", method="GET") == [settings.public_resource_id]


async def test_studio_asset_public_alone_despite_protected_route_row(monkeypatch):
    # The always-public pattern tier is AUTHORITATIVE, not additive: a studio-asset path
    # that ALSO carries a protected route row resolves the public id ALONE (the row's id
    # dropped), so the door opens. An additive grant would leave {public, row-id} — deny
    # wins (CASE B) — and the ESM-imported bundle, which cannot send an auth header, 401s.
    settings = AccessControlSettings()
    pg = FakeAccessControlPg()
    pg.add_route("/api/plugins/x/studio/main-abc123.js", "plugins-scope")
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/plugins/x/studio/main-abc123.js", method="GET") == [
        settings.public_resource_id
    ]


async def test_studio_pattern_under_reserved_prefix_denied_even_with_row(monkeypatch):
    # The authoritative carve-out never fires for a reserved path: a pattern that matches
    # a reserved control-plane path grants nothing even when a route row pins it public —
    # it falls through to the reserved-drop and, being reserved, resolves to nothing.
    settings = AccessControlSettings(always_public_route_patterns=(r"/api/auth/deep/secret",))
    pg = FakeAccessControlPg()
    pg.add_route("/api/auth/deep/secret", settings.public_resource_id)
    _wire(monkeypatch, pg)
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/auth/deep/secret", method="GET") == []


def test_always_public_route_patterns_reserved_rejected():
    # A pattern that can match a reserved control-plane path is rejected at construction:
    # a public pattern cannot target the never-public surface.
    with pytest.raises(ValueError, match="never-public control plane"):
        AccessControlSettings(always_public_route_patterns=(r"/api/auth/.+",))


async def test_always_public_pattern_runtime_reserved_drop_backstop(monkeypatch):
    # Defense in depth: a pattern the construction guard's probes miss but that still
    # matches a reserved path is dropped at runtime — never served public.
    settings = AccessControlSettings(always_public_route_patterns=(r"/api/auth/deep/secret",))
    _wire(monkeypatch, FakeAccessControlPg())
    v = _verifier(settings)
    assert await v.resolve_resource_ids("/api/auth/deep/secret", method="GET") == []


def test_always_public_route_patterns_invalid_regex_raises():
    with pytest.raises(ValueError, match="not a valid regex"):
        AccessControlSettings(always_public_route_patterns=(r"/api/plugins/[unclosed",))


# -- declared-protection tier: a registered authed route with no row → universal scope --


async def test_registered_authed_route_with_no_rows_resolves_universal_scope(monkeypatch):
    """A fresh deployment has an empty route table. A registered, AUTHENTICATED /api
    route the operator mapped to no scope must resolve to the universal scope "*"
    (which every role-holder carries), not to [] — [] fails closed for every identity
    but the super-admin, which is the fresh-deploy bug. GET /api/tools is such a route
    (authed=True). A SCOPED key then still needs an operator row (it lacks "*")."""
    v = _verifier()
    _wire(monkeypatch, FakeAccessControlPg(), FakeRedis())
    assert await v.resolve_resource_ids("/api/tools", method="GET") == ["*"]


async def test_declared_protection_tier_requires_a_method(monkeypatch):
    """Method-less resolution (a websocket scope, a batch pre-read) keeps today's
    behaviour: no method → the tier cannot read the served surface → []."""
    v = _verifier()
    _wire(monkeypatch, FakeAccessControlPg(), FakeRedis())
    assert await v.resolve_resource_ids("/api/tools") == []


async def test_unregistered_api_path_still_fails_closed(monkeypatch):
    """A path the application does not serve resolves to [] — CASE A fail-closed keeps
    its meaning (super-admin only); the tier claims only REGISTERED authed surfaces."""
    v = _verifier()
    _wire(monkeypatch, FakeAccessControlPg(), FakeRedis())
    assert await v.resolve_resource_ids("/api/does-not-exist", method="GET") == []


async def test_declared_protection_tier_claims_a_mounted_transport_from_the_registry(monkeypatch):
    """A registered MOUNTED transport (an MCP/SSE/sub-MCP surface) the operator mapped to no
    row resolves to the universal scope via the registry-derived mounted matcher. The matcher
    is built from the LIVE registry pass, so a CONFIGURED (non-default) transport path is
    covered with no hard-coded literal. A GET here would otherwise fall to the public SPA shell
    (it is not under /api or /mcp); the tier claims it as an authenticated surface instead —
    the signed-off transport tightening. A method the mount does not serve stays unresolved."""
    from tai42_skeleton.access_control import role_gate
    from tai42_skeleton.app.route_registry.registry import RouteRegistry

    tmp = RouteRegistry()
    tmp.record_mounted(path="/xport-sse", methods=["GET"], name="x-sse", summary="transport")
    # A TEMPLATED sub-MCP mount (``/xport-app/{path:path}``) — a concrete GET beneath it must
    # also win over the catch-all (the sub-MCP GET exposure the fix closes).
    tmp.record_mounted(path="/xport-app/{path:path}", methods=["GET"], name="x-app", summary="sub-mcp")
    sse_mount = next(m for m in tmp.routes() if m.path == "/xport-sse")
    app_mount = next(m for m in tmp.routes() if m.path == "/xport-app/{path:path}")
    # a mount is always an authenticated surface
    assert sse_mount.mounted
    assert sse_mount.authed
    assert app_mount.mounted
    assert app_mount.authed

    # Include the REAL registry (which carries the public SPA catch-all ``/{spa_path:path}``
    # that matches every GET path) ALONGSIDE the mounts — a mount must win over the
    # catch-all, else a GET transport stays shell-public (the fresh-deploy transport bug).
    real_routes = list(role_gate.load_all_routes())
    assert any(m.public and "{" in m.path for m in real_routes), "expected the SPA catch-all in the registry"
    monkeypatch.setattr(role_gate, "load_all_routes", lambda: [*real_routes, sse_mount, app_mount])
    role_gate.reset_route_index()
    try:
        # The served-surface resolver returns the MOUNT, not the catch-all, for the GET.
        served = role_gate.resolve_served_surface("/xport-sse", "GET")
        assert served is not None
        assert served.mounted
        assert served.authed

        v = _verifier()
        _wire(monkeypatch, FakeAccessControlPg(), FakeRedis())
        # Concrete mount and a concrete path beneath the templated mount both → universal scope.
        assert await v.resolve_resource_ids("/xport-sse", method="GET") == ["*"]
        assert await v.resolve_resource_ids("/xport-app/some/tool", method="GET") == ["*"]
        # A method the mount does not serve is not a served surface → nothing (CASE A).
        assert await v.resolve_resource_ids("/xport-sse", method="POST") == []
    finally:
        role_gate.reset_route_index()
