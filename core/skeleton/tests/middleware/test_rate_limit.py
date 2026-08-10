"""The app-level ``RateLimitMiddleware``: it derives its coverage from the route
registry (every ``authed=False`` route is throttled, a plugin's new public door
included), groups doors into families with disjoint budgets, leaves authed routes —
the mounted MCP transport surfaces among them — and unregistered paths untouched,
honours the shipped/operator budget overrides, and trips both the burst and the
per-minute window."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tai42_skeleton.app.route_registry import RouteMetadata
from tai42_skeleton.middleware import rate_limit
from tai42_skeleton.settings.audit_log import AuditLogSettings
from tai42_skeleton.settings.rate_limit import FamilyOverride, RateLimitSettings

# Import the fake as a package-relative module (tests is a package).
from tests._fakes.interactions_redis import FakeRedis

# A refusal audit line; the trailing ``reason=`` group is absent for a 429 (the
# limiter carries no DenialCause).
_REJECT_LINE = re.compile(
    r"^audit: principal=(?P<principal>\S+) method=(?P<method>\S+) route=(?P<route>\S+) "
    r"status=(?P<status>\d+) duration_ms=(?P<duration_ms>\d+) ts=(?P<ts>\S+)"
    r"(?: reason=(?P<reason>\S+))?$"
)


async def _ok(request):
    return PlainTextResponse("ok")


def _meta(path: str, methods: list[str], *, authed: bool) -> RouteMetadata:
    """One registry entry with only the fields the limiter reads carrying meaning."""
    return RouteMetadata(
        path=path,
        methods=tuple(sorted(m.upper() for m in methods)),
        name=path,
        summary="s",
        description="",
        tags=("t",),
        authed=authed,
        request_model=None,
        response_model=None,
        reload_gated=False,
        reads_body=False,
        error_statuses=(),
        success_status=200,
        additional_success_statuses=(),
        success_media_types={},
        action="read",
    )


# The registered surface the tests match against: the skeleton's own public doors, an
# authed family, the Studio SPA catch-all, and a plugin door the limiter has never
# heard of.
_SURFACE: list[RouteMetadata] = [
    _meta("/universal_webhook/{topic}", ["GET", "POST"], authed=False),
    _meta("/api/interactions/callback/{ticket}", ["GET", "POST"], authed=False),
    _meta("/trigger/{token}", ["GET", "POST"], authed=False),
    _meta("/api/channels/web/chat/{identity}", ["GET"], authed=False),
    _meta("/api/channels/web/messages", ["POST"], authed=False),
    _meta("/api/channels/newvendor/inbound", ["POST"], authed=False),
    _meta("/health", ["GET"], authed=False),
    _meta("/api/hooks", ["GET"], authed=True),
    _meta("/api/hooks/trigger-links", ["GET"], authed=True),
    _meta("/{spa_path:path}", ["GET"], authed=False),
]

_ROUTES = [
    Route("/universal_webhook/{topic}", _ok, methods=["GET", "POST"]),
    Route("/api/interactions/callback/{ticket}", _ok, methods=["GET", "POST"]),
    Route("/trigger/{token}", _ok, methods=["GET", "POST"]),
    Route("/api/channels/web/chat/{identity}", _ok, methods=["GET"]),
    Route("/api/channels/web/messages", _ok, methods=["POST"]),
    Route("/api/channels/newvendor/inbound", _ok, methods=["POST"]),
    Route("/health", _ok, methods=["GET"]),
    Route("/api/hooks", _ok, methods=["GET"]),
    Route("/api/hooks/trigger-links", _ok, methods=["GET"]),
    # The MOUNTED transport surfaces, which no ROUTER registers: the app records them
    # itself as it mounts them (``_transport_surface``).
    Route("/mcp", _ok, methods=["GET", "POST", "DELETE"]),
    Route("/sse", _ok, methods=["GET"]),
    Route("/messages/{path:path}", _ok, methods=["GET", "POST"]),
    Route("/app/{path:path}", _ok, methods=["GET", "POST"]),
    Route("/{spa_path:path}", _ok, methods=["GET"]),
]


@pytest.fixture(autouse=True)
def _registered_surface(monkeypatch):
    """Every test matches against ``_SURFACE`` rather than the live registry, and each
    starts from a cold door table (the memoized one is keyed on the registry version,
    which a swapped-in surface does not move)."""
    monkeypatch.setattr(rate_limit, "load_all_routes", lambda: list(_SURFACE))
    rate_limit._reset_door_table_cache()
    yield
    rate_limit._reset_door_table_cache()


def _mount_registry(monkeypatch):
    """A throwaway registry the PRODUCTION mount-recording calls write into, so the
    coverage they buy is only ever as real as that registration: a build that stops
    recording a mounted surface — or records it public — leaves the SPA catch-all
    claiming those paths and turns the test red."""
    from tai42_skeleton.app import http as app_http
    from tai42_skeleton.app import server as app_server
    from tai42_skeleton.app.route_registry import RouteRegistry

    registry = RouteRegistry()
    monkeypatch.setattr(app_server, "route_registry", registry)
    monkeypatch.setattr(app_http, "route_registry", registry)
    return registry


def _match_against_mounts(monkeypatch, registry) -> None:
    """Match every request against ``_SURFACE`` plus whatever the mount recording left in
    ``registry``."""
    monkeypatch.setattr(rate_limit, "load_all_routes", lambda: [*_SURFACE, *registry.routes()])
    rate_limit._reset_door_table_cache()


@pytest.fixture
def _transport_surface(monkeypatch):
    """``_SURFACE`` plus the records the APP ITSELF lays down as it mounts its MCP
    transports and its sub-MCP router, for the STATEFUL gated deployment."""
    from tai42_skeleton.app import http as app_http
    from tai42_skeleton.app import server as app_server

    registry = _mount_registry(monkeypatch)
    app_server.record_streamable_http_surface("/mcp", stateless=False)
    app_server.record_sse_surface("/sse", "/messages")
    app_http.record_sub_mcp_mount("/app")
    _match_against_mounts(monkeypatch, registry)
    yield
    rate_limit._reset_door_table_cache()


@pytest.fixture(autouse=True)
def _rate_limit_redis_configured(monkeypatch):
    # rate limiting is OFF (pass-through) with no Redis. These tests exercise the
    # ON limiter, so configure its Redis — the fake ``client_ctx`` still stands in for
    # the connection; only the presence gate in the middleware reads this.
    monkeypatch.setenv("TAI_RATE_LIMIT_REDIS_URL", "redis://localhost:6379/0")


def _build_client(monkeypatch, settings: RateLimitSettings, fake: FakeRedis, *, peer: str = "testclient") -> TestClient:
    monkeypatch.setattr(rate_limit, "rate_limit_settings", lambda: settings)
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: 100.0))
    # The shipped per-family budgets are stood down for the request-level tests: they
    # tune the DEFAULT budget and assert the resolution around it, so a shipped 60/10
    # would quietly supply the numbers instead. The shipped table has its own tests.
    monkeypatch.setattr("tai42_skeleton.settings.rate_limit.SHIPPED_FAMILY_BUDGETS", {})

    @asynccontextmanager
    async def _ctx(cls, s=None, *, fresh=False, **kw):
        yield fake

    monkeypatch.setattr(rate_limit, "client_ctx", _ctx)
    return TestClient(rate_limit.RateLimitMiddleware(Starlette(routes=_ROUTES)), client=(peer, 50000))


def _settings(**overrides) -> RateLimitSettings:
    """Settings whose every family bursts at 2, so three requests prove a limit."""
    base: dict[str, Any] = {"default_burst": 2, "default_limit": 1000}
    base.update(overrides)
    return RateLimitSettings(**base)


# -- family derivation -------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/universal_webhook/{topic}", "universal_webhook"),
        ("/trigger/{token}", "trigger"),
        ("/api/interactions/callback/{ticket}", "interactions_callback"),
        ("/api/channels/web/chat/{identity}", "channels_web"),
        ("/api/channels/web/messages", "channels_web"),
        ("/api/channels/web/questions/{interaction_id}/answer", "channels_web"),
        ("/api/channels/slack/inbound", "channels_slack"),
        ("/api/login/password", "login_password"),
        ("/health", "health"),
        ("/api/plugins/{name}/studio/{path:path}", "plugins"),
        ("/{spa_path:path}", "root"),
        ("/", "root"),
    ],
)
def test_family_of_derives_the_door_stem(path: str, expected: str):
    assert rate_limit.family_of(path) == expected


# -- coverage comes from the registry ----------------------------------------


def test_webhook_family_limited_after_burst(monkeypatch):
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    statuses = [client.get("/universal_webhook/events").status_code for _ in range(3)]
    assert statuses[0] == 200
    assert statuses[-1] == 429


def test_interactions_callback_family_limited_after_burst(monkeypatch):
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    statuses = [client.post("/api/interactions/callback/TKT").status_code for _ in range(3)]
    assert statuses[-1] == 429


def test_trigger_family_limited_after_burst(monkeypatch):
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    statuses = [client.get("/trigger/tok").status_code for _ in range(3)]
    assert statuses[0] == 200
    assert statuses[-1] == 429


def test_a_public_door_the_limiter_never_heard_of_is_throttled(monkeypatch):
    # THE point of deriving coverage: a plugin's public door is throttled on the
    # strength of its ``authed=False`` registration alone — no prefix, no setting, no
    # entry anywhere in this package names it.
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    statuses = [client.post("/api/channels/newvendor/inbound").status_code for _ in range(3)]
    assert statuses[0] == 200
    assert statuses[-1] == 429


def test_a_new_public_door_has_its_own_budget(monkeypatch):
    # ... and it is its OWN family, so exhausting it leaves the webhook door alone.
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    for _ in range(3):
        client.post("/api/channels/newvendor/inbound")
    assert client.post("/api/channels/newvendor/inbound").status_code == 429
    assert client.get("/universal_webhook/events").status_code == 200


def test_public_health_door_is_throttled(monkeypatch):
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    statuses = [client.get("/health").status_code for _ in range(3)]
    assert statuses[-1] == 429


def test_spa_catch_all_is_the_root_family(monkeypatch):
    # The SPA shell is a public door like any other; its family is the stem-less root.
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    for _ in range(3):
        client.get("/settings/profiles")
    assert client.get("/some/other/deep/link").status_code == 429


def test_authed_route_never_limited(monkeypatch):
    # The public SPA catch-all textually covers /api/hooks too; the more specific
    # AUTHED route must win, or the limiter would throttle the authed surface.
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    statuses = [client.get("/api/hooks").status_code for _ in range(10)]
    assert set(statuses) == {200}


def test_authed_trigger_links_crud_never_limited(monkeypatch):
    # The authed management routes are NOT the public /trigger/ family.
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    statuses = [client.get("/api/hooks/trigger-links").status_code for _ in range(10)]
    assert set(statuses) == {200}


def test_unregistered_path_passes_through(monkeypatch):
    # A POST no route in the registered surface describes is no declared public door:
    # the limiter never touches it.
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    statuses = [client.post("/mcp").status_code for _ in range(10)]
    assert set(statuses) == {200}


@pytest.mark.usefixtures("_transport_surface")
@pytest.mark.parametrize("path", ["/mcp", "/sse", "/messages/x", "/app/alpha"])
def test_a_mounted_transport_surface_is_never_the_public_catch_all(monkeypatch, path: str):
    # The SPA catch-all matches EVERY GET, the mounted transport paths included — so
    # without their own records the limiter would charge credential-gated MCP traffic to
    # the public root family and audit its refusals as UNAUTHENTICATED. The app records
    # what it mounts, and a mounted surface is authed, so the limiter passes it through.
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    assert {client.get(path).status_code for _ in range(10)} == {200}


@pytest.mark.usefixtures("_transport_surface")
def test_the_mcp_transport_passes_through_on_its_write_methods_too(monkeypatch):
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    assert {client.post("/mcp").status_code for _ in range(10)} == {200}


def test_a_stateless_transport_leaves_its_get_to_the_public_catch_all(monkeypatch):
    # fastmcp binds no GET on a STATELESS endpoint — there is no session to stream from —
    # so the recorder must claim none either. A phantom authed GET door outranks the SPA
    # catch-all and would leave GET/HEAD /mcp unthrottled on a surface nothing serves.
    from tai42_skeleton.app import server as app_server

    registry = _mount_registry(monkeypatch)
    app_server.record_streamable_http_surface("/mcp", stateless=True)
    _match_against_mounts(monkeypatch, registry)
    client = _build_client(monkeypatch, _settings(), FakeRedis())

    statuses = [client.get("/mcp").status_code for _ in range(3)]
    assert statuses[0] == 200
    assert statuses[-1] == 429
    # ... charged to the ROOT family it fell into, never a budget of its own.
    assert client.get("/settings/profiles").status_code == 429
    # The methods a stateless endpoint DOES bind are recorded and pass through.
    assert {client.post("/mcp").status_code for _ in range(10)} == {200}
    assert {client.delete("/mcp").status_code for _ in range(10)} == {200}


def test_an_epoch_mounting_a_narrower_method_set_leaves_no_wider_door_behind(monkeypatch):
    # Every epoch re-records what it mounts. A mounted record keyed on the method set as
    # well as the path would ACCUMULATE across a settings change: the stateful epoch's
    # GET,POST,DELETE door would survive the stateless rebuild, outrank the SPA catch-all
    # and leave GET /mcp unthrottled on a surface this deployment no longer serves.
    from tai42_skeleton.app import server as app_server

    registry = _mount_registry(monkeypatch)
    app_server.record_streamable_http_surface("/mcp", stateless=False)
    app_server.record_streamable_http_surface("/mcp", stateless=True)
    assert [meta.methods for meta in registry.routes()] == [("DELETE", "POST")]

    _match_against_mounts(monkeypatch, registry)
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    assert [client.get("/mcp").status_code for _ in range(3)][-1] == 429
    assert {client.post("/mcp").status_code for _ in range(10)} == {200}


@pytest.mark.usefixtures("_transport_surface")
def test_the_public_catch_all_still_governs_everything_the_mounts_do_not_serve(monkeypatch):
    # The mounted records exempt the mount paths ALONE: the shell surface around them is
    # the public root family it always was.
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    for _ in range(3):
        client.get("/settings/profiles")
    assert client.get("/some/other/deep/link").status_code == 429


def test_head_on_a_public_get_door_is_throttled(monkeypatch):
    # Starlette answers HEAD from a GET route, so the limiter must too: a table built
    # from the DECLARED methods alone would leave every public GET door open to a HEAD
    # flood that reaches the same handler.
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    statuses = [client.head("/trigger/tok").status_code for _ in range(3)]
    assert statuses[0] == 200
    assert statuses[-1] == 429


def test_a_route_recorded_after_the_first_request_is_covered(monkeypatch):
    # The door table is memoized against the registry version. A reload that records a
    # new public door moves that version, and THAT is what makes the next request
    # rebuild — without it the limiter keeps matching against the previous deployment's
    # doors and the new door is unthrottled for the life of the process.
    surface = list(_SURFACE)
    registry = SimpleNamespace(version=1)
    monkeypatch.setattr(rate_limit, "load_all_routes", lambda: list(surface))
    monkeypatch.setattr(rate_limit, "route_registry", registry)
    client = _build_client(monkeypatch, _settings(), FakeRedis())

    # Not yet a declared door: the limiter passes the request on to the app, however many
    # times it is repeated.
    assert 429 not in {client.post("/api/channels/reloaded/inbound").status_code for _ in range(10)}

    surface.append(_meta("/api/channels/reloaded/inbound", ["POST"], authed=False))
    registry.version += 1
    statuses = [client.post("/api/channels/reloaded/inbound").status_code for _ in range(3)]
    assert statuses[0] != 429  # the first hit is within budget and still reaches the app
    assert statuses[-1] == 429


def test_the_door_table_is_stamped_with_the_pre_build_registry_version(monkeypatch):
    # The version is read BEFORE the table compiles, so the memo can only ever
    # UNDER-claim. A door can be recorded DURING a build (``load_all_routes`` imports
    # router modules, and an epoch build records on its own thread): stamping the memo
    # with the version read AFTER the build would claim doors the table does not hold,
    # and that door would stay unthrottled for the life of the process.
    registry = SimpleNamespace(version=1)
    built_at: list[int] = []

    def _load():
        built_at.append(registry.version)
        registry.version += 1  # a route recorded while this build is compiling
        return list(_SURFACE)

    monkeypatch.setattr(rate_limit, "load_all_routes", _load)
    monkeypatch.setattr(rate_limit, "route_registry", registry)
    rate_limit._reset_door_table_cache()

    rate_limit._door_for("/health", "GET")
    assert rate_limit._door_table is not None
    assert rate_limit._door_table[0] == 1  # the PRE-build version, never the post-build 2
    # ... so the next request sees the memo lagging the registry and rebuilds against the
    # surface that grew mid-build.
    rate_limit._door_for("/health", "GET")
    assert built_at == [1, 2]


@pytest.mark.parametrize(
    "budget",
    [
        pytest.param({"default_burst": 2, "default_limit": 1000}, id="burst-window"),
        pytest.param({"default_burst": 1000, "default_limit": 2}, id="minute-window"),
    ],
)
def test_two_clients_do_not_share_one_budget(monkeypatch, budget: dict[str, int]):
    # BOTH window counters key on the client bucket: one address exhausting a family must
    # leave every other address its own budget, or a single flooder locks the door for
    # the world.
    fake = FakeRedis()
    settings = _settings(**budget)
    flooder = _build_client(monkeypatch, settings, fake, peer="203.0.113.7")
    bystander = _build_client(monkeypatch, settings, fake, peer="203.0.113.8")
    assert [flooder.get("/trigger/tok").status_code for _ in range(3)][-1] == 429
    assert bystander.get("/trigger/tok").status_code == 200


def test_web_chat_family_shares_one_budget_across_its_doors(monkeypatch):
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    client.get("/api/channels/web/chat/site-alpha")
    client.post("/api/channels/web/messages")
    assert client.post("/api/channels/web/messages").status_code == 429


def test_families_have_disjoint_budgets(monkeypatch):
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    for _ in range(3):
        client.get("/universal_webhook/events")
    assert client.get("/universal_webhook/events").status_code == 429
    # The callback family still has its own budget.
    assert client.post("/api/interactions/callback/TKT").status_code == 200


# -- budgets: default, shipped, operator override ----------------------------


def test_disabled_family_passes_through(monkeypatch):
    client = _build_client(
        monkeypatch, _settings(families={"universal_webhook": FamilyOverride(enabled=False)}), FakeRedis()
    )
    statuses = [client.get("/universal_webhook/events").status_code for _ in range(10)]
    assert set(statuses) == {200}
    # Disabling one family leaves every other one charged.
    assert [client.get("/trigger/tok").status_code for _ in range(3)][-1] == 429


def test_default_enabled_false_turns_every_family_off(monkeypatch):
    client = _build_client(monkeypatch, _settings(default_enabled=False), FakeRedis())
    for path in ("/universal_webhook/events", "/trigger/tok", "/health"):
        assert {client.get(path).status_code for _ in range(10)} == {200}


def test_operator_override_widens_one_family_only(monkeypatch):
    client = _build_client(monkeypatch, _settings(families={"trigger": FamilyOverride(burst=20)}), FakeRedis())
    assert [client.get("/trigger/tok").status_code for _ in range(6)][-1] == 200
    assert [client.get("/universal_webhook/events").status_code for _ in range(3)][-1] == 429


def test_override_field_falls_through_to_the_default():
    # An override naming only the limit keeps the default burst — each field resolves
    # on its own, so tuning one window never silently resets the other.
    settings = RateLimitSettings(
        default_limit=600, default_burst=120, families={"channels_newvendor": FamilyOverride(limit=5)}
    )
    budget = settings.budget_for("channels_newvendor")
    assert (budget.limit, budget.burst, budget.enabled) == (5, 120, True)


def test_shipped_budget_applies_with_no_configuration():
    # The doors that ship a tighter budget keep it with an empty env ...
    settings = RateLimitSettings()
    assert settings.budget_for("trigger") == settings.budget_for("universal_webhook")
    assert (settings.budget_for("trigger").limit, settings.budget_for("trigger").burst) == (60, 10)
    assert (settings.budget_for("channels_web").limit, settings.budget_for("channels_web").burst) == (120, 30)
    # ... and a family with no shipped entry gets the wide default backstop.
    assert (settings.budget_for("channels_newvendor").limit, settings.budget_for("channels_newvendor").burst) == (
        600,
        120,
    )


def test_operator_override_beats_the_shipped_budget():
    settings = RateLimitSettings(families={"trigger": FamilyOverride(limit=9)})
    assert settings.budget_for("trigger").limit == 9
    assert settings.budget_for("trigger").burst == 10  # the shipped burst survives


def test_family_override_reads_from_env_json(monkeypatch):
    monkeypatch.setenv("TAI_RATE_LIMIT_FAMILIES", '{"trigger": {"limit": 7, "enabled": false}}')
    settings = RateLimitSettings()
    assert settings.budget_for("trigger").limit == 7
    assert settings.budget_for("trigger").enabled is False


def test_family_override_reads_from_nested_env(monkeypatch):
    monkeypatch.setenv("TAI_RATE_LIMIT_FAMILIES__TRIGGER__LIMIT", "11")
    settings = RateLimitSettings()
    assert settings.budget_for("trigger").limit == 11
    assert settings.budget_for("trigger").burst == 10  # the shipped burst still applies


def test_family_override_key_case_is_folded(monkeypatch):
    # Env-set keys arrive in whatever case the operator typed; the derived family name is
    # always lower-case, so an upper-case override must still find its door.
    monkeypatch.setenv("TAI_RATE_LIMIT_FAMILIES", '{"TRIGGER": {"limit": 4}}')
    assert RateLimitSettings().budget_for("trigger").limit == 4


def test_an_override_cannot_open_a_door_the_registry_never_declared(monkeypatch):
    # An override TUNES a family; it can never decide that something is covered. No door
    # in the registered surface answers a POST to /mcp, so naming its family — even at a
    # limit of one — leaves the request passing through.
    client = _build_client(monkeypatch, _settings(families={"mcp": FamilyOverride(limit=1, burst=1)}), FakeRedis())
    assert {client.post("/mcp").status_code for _ in range(10)} == {200}


def test_unknown_override_field_is_refused(monkeypatch):
    # A typo cannot become a silently-ignored setting an operator believes is applied.
    monkeypatch.setenv("TAI_RATE_LIMIT_FAMILIES", '{"trigger": {"limitt": 7}}')
    with pytest.raises(ValueError, match="limitt"):
        RateLimitSettings()


def test_redis_url_still_reads_its_own_env(monkeypatch):
    # The nested-delimiter config the family overrides need must not disturb the
    # composed redis connection group.
    monkeypatch.setenv("TAI_RATE_LIMIT_REDIS_URL", "redis://elsewhere:6379/3")
    assert RateLimitSettings().redis.redis_url == "redis://elsewhere:6379/3"


def test_any_family_enabled_tracks_the_switches():
    assert RateLimitSettings().any_family_enabled() is True
    assert RateLimitSettings(default_enabled=False).any_family_enabled() is False
    assert (
        RateLimitSettings(
            default_enabled=False, families={"trigger": FamilyOverride(enabled=True)}
        ).any_family_enabled()
        is True
    )
    assert (
        RateLimitSettings(default_enabled=False, families={"trigger": FamilyOverride(limit=5)}).any_family_enabled()
        is False
    )


# -- refusal shape -----------------------------------------------------------


def test_a_429_emits_one_refusal_audit_line_marked_unauthenticated(monkeypatch, caplog):
    # The limiter rejects at the OUTER door, before the gate: the refusal audit line is
    # unauthenticated, records the matched route's TEMPLATE (never the token riding in
    # its parameter), and carries no DenialCause reason.
    monkeypatch.setattr(rate_limit, "audit_log_settings", lambda: AuditLogSettings())
    client = _build_client(monkeypatch, _settings(), FakeRedis())

    secret = "supersecret-token"
    with caplog.at_level(logging.INFO):
        statuses = [client.get(f"/trigger/{secret}").status_code for _ in range(3)]
    assert statuses[-1] == 429

    audit_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("audit: ")]
    assert len(audit_lines) == 1, audit_lines  # only the 429 audits; the 200 passes do not
    match = _REJECT_LINE.fullmatch(audit_lines[0])
    assert match is not None, audit_lines[0]
    assert match.group("principal") == "unauthenticated"
    assert match.group("status") == "429"
    assert match.group("route") == "/trigger/{token}"
    assert match.group("reason") is None
    # Scoped to the audit module's own records (the TestClient's HTTP logger names the
    # URL by design); message AND raw args, so a secret riding as an argument is caught.
    haystack = "\n".join(
        r.getMessage() + repr(r.args) for r in caplog.records if r.name == "tai42_skeleton.middleware.audit_log"
    )
    assert secret not in haystack


def test_retry_after_header_present_on_429(monkeypatch):
    client = _build_client(monkeypatch, _settings(), FakeRedis())
    resp = None
    for _ in range(3):
        resp = client.get("/universal_webhook/events")
    assert resp is not None
    assert resp.status_code == 429
    assert "retry-after" in resp.headers


def test_minute_window_trips_with_bounded_retry_after(monkeypatch):
    # A low per-minute limit under a high burst ceiling trips the MINUTE window (not
    # the burst), returning 429 with a Retry-After bounded by the 60s window.
    client = _build_client(monkeypatch, _settings(default_limit=3, default_burst=100), FakeRedis())
    statuses = [client.get("/universal_webhook/events").status_code for _ in range(4)]
    assert statuses[:3] == [200, 200, 200]
    resp = client.get("/universal_webhook/events")
    assert resp.status_code == 429
    assert 0 < int(resp.headers["retry-after"]) <= 60


def test_rate_limit_config_owns_the_public_door_budgets():
    # The public-door limiter's config lives on the app-level ``RateLimitSettings``,
    # and the ``InteractionsSettings`` carries no limiter fields of its own.
    from tai42_skeleton.interactions.settings import InteractionsSettings

    rate_fields = set(RateLimitSettings.model_fields)
    for field in ("default_limit", "default_burst", "default_enabled", "families"):
        assert field in rate_fields

    interactions_fields = set(InteractionsSettings.model_fields)
    for field in ("callback_rate_limit_per_minute", "callback_rate_burst", "callback_trusted_proxies"):
        assert field not in interactions_fields


# -- OFF (no Redis) → pass-through + one boot WARNING --------------------


def test_no_redis_passes_through_every_door_with_zero_client_ctx(monkeypatch):
    # OFF (no Redis anywhere): every public door flows straight through unthrottled and
    # the limiter opens no client.
    monkeypatch.delenv("TAI_RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    settings = _settings()
    assert settings.redis.redis_url is None

    calls: list = []

    @asynccontextmanager
    async def _ctx(cls, s=None, *, fresh=False, **kw):
        calls.append(cls)
        yield FakeRedis()

    monkeypatch.setattr(rate_limit, "rate_limit_settings", lambda: settings)
    monkeypatch.setattr(rate_limit, "client_ctx", _ctx)
    client = TestClient(rate_limit.RateLimitMiddleware(Starlette(routes=_ROUTES)))
    for path in ("/universal_webhook/x", "/api/interactions/callback/x", "/trigger/x", "/health"):
        for _ in range(50):  # far past any burst/minute budget
            assert client.get(path).status_code == 200
    assert calls == []  # the counter store was never opened


def test_boot_warning_fires_exactly_once_across_two_boots(monkeypatch, caplog):
    monkeypatch.delenv("TAI_RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    monkeypatch.setattr(rate_limit, "_RATE_LIMIT_OFF_WARNED", False)
    log = logging.getLogger("test.rate_limit_off")
    with caplog.at_level(logging.WARNING, logger=log.name):
        rate_limit.warn_if_rate_limiting_off(log)
        rate_limit.warn_if_rate_limiting_off(log)  # second boot/reload — no second line
    fired = [r for r in caplog.records if "rate limiting: OFF" in r.getMessage()]
    assert len(fired) == 1


def test_boot_warning_silent_when_redis_configured(monkeypatch, caplog):
    # Redis IS configured (the autouse fixture), so no OFF warning ever fires.
    monkeypatch.setattr(rate_limit, "_RATE_LIMIT_OFF_WARNED", False)
    monkeypatch.setenv("TAI_RATE_LIMIT_TRUSTED_HOPS", "1")
    log = logging.getLogger("test.rate_limit_on")
    with caplog.at_level(logging.WARNING, logger=log.name):
        rate_limit.warn_if_rate_limiting_off(log)
    assert not [r for r in caplog.records if "rate limiting: OFF" in r.getMessage()]


def test_boot_validates_trusted_proxy_roster_even_with_no_redis(monkeypatch):
    # The trusted-proxy roster is validated at boot UNCONDITIONALLY — a malformed
    # TAI_RATE_LIMIT_TRUSTED_PROXIES is refused even on a Redis-less deployment, where
    # the limiter is OFF. A typo must not sit silent until Redis is configured and then
    # collapse every client into one shared bucket.
    monkeypatch.delenv("TAI_RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    monkeypatch.setattr(rate_limit, "_RATE_LIMIT_OFF_WARNED", False)
    monkeypatch.setenv("TAI_RATE_LIMIT_TRUSTED_PROXIES", '["not-an-ip"]')
    log = logging.getLogger("test.rate_limit_roster")
    with pytest.raises(ValueError, match="neither an IP address nor a CIDR block"):
        rate_limit.warn_if_rate_limiting_off(log)


def test_boot_validates_trusted_proxy_roster_on_reload_after_first_boot(monkeypatch):
    # The roster validator fires on EVERY call, not just the process's first boot. With
    # the once-per-process OFF guard already tripped (a reload after the first Redis-less
    # boot), a malformed TAI_RATE_LIMIT_TRUSTED_PROXIES must STILL be refused. Guarding
    # the validation behind the OFF dedup would let a typo introduced on a later reload
    # sit silent until Redis is configured and then collapse every client into one bucket.
    monkeypatch.delenv("TAI_RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    monkeypatch.setattr(rate_limit, "_RATE_LIMIT_OFF_WARNED", True)
    monkeypatch.setenv("TAI_RATE_LIMIT_TRUSTED_PROXIES", '["not-an-ip"]')
    log = logging.getLogger("test.rate_limit_roster_reload")
    with pytest.raises(ValueError, match="neither an IP address nor a CIDR block"):
        rate_limit.warn_if_rate_limiting_off(log)


def test_boot_warns_when_the_limiter_is_on_but_no_proxy_trust_is_declared(monkeypatch, caplog):
    # ON with no trust statement: X-Forwarded-For is ignored, which buckets the whole
    # world together behind a proxy. Said at boot, not discovered under a flood.
    monkeypatch.setattr(rate_limit, "_RATE_LIMIT_OFF_WARNED", False)
    monkeypatch.delenv("TAI_RATE_LIMIT_TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("TAI_RATE_LIMIT_TRUSTED_HOPS", raising=False)
    log = logging.getLogger("test.rate_limit_trust")
    with caplog.at_level(logging.WARNING, logger=log.name):
        rate_limit.warn_if_rate_limiting_off(log)
    assert [r for r in caplog.records if "no proxy trust declared" in r.getMessage()]
