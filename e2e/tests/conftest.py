"""Harness servers and the stack-profile fixtures, plus the opt-in collection gates.

Session infra, the ``fresh_stack`` factory, variant-run identification, and the
failure-diagnostics hook live in the published ``tai42_e2e.pytest_plugin`` (loaded
via its pytest11 entry point); this conftest wires only the profiles and net stubs
specific to this repo's suites. Stacks are module-scoped (built lazily, torn down
at module end) so the ~5-6 profiles cap the boot count within the speed budget.
Every stack is isolated by a per-stack Postgres database (a template clone) and a
Redis logical DB, so tests in one module never see another's state."""

from __future__ import annotations

import base64
import secrets
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from tai42_e2e import diagnostics
from tai42_e2e.booting import allocate_and_build, boot_stack
from tai42_e2e.harness import (
    seed_admin_bypass_authz,
    seed_bridge_authz,
    seed_payments_authz,
    seed_projection_authz,
)
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import (
    POSTGRES_MCP_PROBE_ROW_NAME,
    POSTGRES_MCP_PROBE_SCHEMA,
    POSTGRES_MCP_PROBE_TABLE,
    build_accounts_stack,
    build_agents_redis_stack,
    build_agents_stack,
    build_api_router_stack,
    build_auth_stack,
    build_bare_stack,
    build_bridge_stack,
    build_channel_stack,
    build_connectors_stack,
    build_core_stack,
    build_default_router_stack,
    build_embed_stack,
    build_extensions_stack,
    build_minimal_stack,
    build_monitoring_stack,
    build_off_stack,
    build_oidc_stack,
    build_payments_stack,
    build_postgres_mcp_stack,
    build_projection_authz_stack,
    build_projection_stack,
    build_recycle_stack,
    build_replicas_stack,
    build_schedule_stack,
    build_shipped_connectors_stack,
)
from tai42_e2e.netfixtures import (
    FakeSlack,
    FakeStripe,
    FakeTelegram,
    FakeTwilio,
    FakeWhatsApp,
    OAuthIdp,
    RecordingConnectProxy,
    TargetServer,
)
from tai42_e2e.pytest_plugin import gated_collect_ignore
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import Infra, StackResources, TaiStack

# The monitoring / marketplace / fleet suites only collect when their opt-in gate is
# on. ``infra`` and ``fresh_stack`` come from the pytest11 plugin.
_gate = HarnessSettings()
collect_ignore_glob: list[str] = gated_collect_ignore(
    {"monitoring": _gate.monitoring, "marketplace": _gate.marketplace, "fleet": _gate.fleet}
)


# ---- harness servers ----------------------------------------------------


@pytest.fixture(scope="session")
def llm_stub() -> Iterator[LlmStub]:
    stub = LlmStub()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture(scope="session")
def target_server() -> Iterator[TargetServer]:
    server = TargetServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def connect_proxy() -> Iterator[RecordingConnectProxy]:
    proxy = RecordingConnectProxy()
    proxy.start()
    try:
        yield proxy
    finally:
        proxy.stop()


@pytest.fixture(scope="session")
def oauth_idp() -> Iterator[OAuthIdp]:
    idp = OAuthIdp()
    idp.start()
    try:
        yield idp
    finally:
        idp.stop()


@pytest.fixture(scope="session")
def fake_telegram() -> Iterator[FakeTelegram]:
    """In-process recording stub of the Telegram Bot API (the channel profile's
    ``CHANNEL_TELEGRAM_API_BASE_URL`` target). Session-scoped like the other net
    fixtures; the channel test modules reset it per test and filter by ``uniq``."""
    stub = FakeTelegram()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture(scope="session")
def fake_slack() -> Iterator[FakeSlack]:
    """In-process recording stub of the Slack Web + Events API."""
    stub = FakeSlack()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture(scope="session")
def fake_twilio() -> Iterator[FakeTwilio]:
    """In-process recording stub of the Twilio REST Messages API."""
    stub = FakeTwilio()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture(scope="session")
def fake_whatsapp() -> Iterator[FakeWhatsApp]:
    """In-process recording stub of the Meta WhatsApp (Graph) API."""
    stub = FakeWhatsApp()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture(scope="session")
def fake_stripe() -> Iterator[FakeStripe]:
    """In-process recording stub of Stripe's Checkout Session REST API (the payments
    stack's ``STRIPE_API_BASE`` target). Session-scoped like the other net fixtures; the
    payment test module resets it between its two tests."""
    stub = FakeStripe()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


# ---- resource allocation + stack boot ----------------------------------

# The allocate-build-boot orchestration lives in the harness library so a per-suite
# conftest can reuse it without a cross-conftest import. Aliased to the private names the
# fixtures below call.
_allocate_and_build = allocate_and_build
_boot = boot_stack


@pytest.fixture(scope="module")
def core_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from _boot(infra, tmp_path_factory.mktemp("core"), build_core_stack)


@pytest.fixture(scope="module")
def default_router_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """A stack booted on the DEFAULT router set (``default_routers="all"`` with no
    ``routers_modules``) — the route-coverage guard asserts every Studio feature
    route the skeleton default-mounts answers non-404 on this bare manifest."""
    yield from _boot(infra, tmp_path_factory.mktemp("default-router"), build_default_router_stack)


@pytest.fixture(scope="module")
def api_router_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """A stack booted HEADLESS (``default_routers="api"`` with no ``routers_modules``)
    — the loader mounts the default API routers but NOT the SPA catch-all. The Q2
    boot test asserts ``/api/*`` answers while ``/`` has no SPA shell."""
    yield from _boot(infra, tmp_path_factory.mktemp("api-router"), build_api_router_stack)


@pytest.fixture(scope="module")
def minimal_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The smallest bootable stack (``default_routers="none"`` with an explicit
    four-router list) — the Q2 boot test uses it as the opt-out archetype: a listed
    router mounts, a defaulted-but-unlisted router (e.g. ``/api/backup``) stays 404."""
    yield from _boot(infra, tmp_path_factory.mktemp("minimal"), build_minimal_stack)


@pytest.fixture(scope="module")
def bare_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """A stack that MOUNTS the storage + backend doors but registers no storage
    provider and no backend — the absent-provider profile the ``present: false`` /
    501 door assertions drive."""
    yield from _boot(infra, tmp_path_factory.mktemp("bare"), build_bare_stack)


@pytest.fixture(scope="module")
def off_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The all-features-OFF profile (D8): the whole default router surface mounted
    but no feature Redis and no default PG, so every DB-backed feature is
    unconfigured. The doctrine spec in ``tests/off`` pins the whole OFF matrix
    against it. No backend, so the module is ``backendless`` (default leg only)."""
    yield from _boot(infra, tmp_path_factory.mktemp("off"), build_off_stack)


@pytest.fixture(scope="module")
def embed_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The embed deployment shape: a user-owned uvicorn process serving a host
    FastAPI app that mounts ``create_app()``, plus one backend-worker sibling."""
    yield from _boot(infra, tmp_path_factory.mktemp("embed"), build_embed_stack)


@pytest.fixture(scope="module")
def replicas_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    resource_kwargs = {
        "gh_webhook_secret": secrets.token_hex(16),
        "stripe_webhook_secret": secrets.token_hex(16),
    }
    yield from _boot(infra, tmp_path_factory.mktemp("replicas"), build_replicas_stack, resource_kwargs=resource_kwargs)


@pytest.fixture(scope="module")
def recycle_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from _boot(infra, tmp_path_factory.mktemp("recycle"), build_recycle_stack)


@pytest.fixture(scope="module")
def auth_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from _boot(infra, tmp_path_factory.mktemp("auth"), build_auth_stack, seed_auth=True)


@pytest.fixture(scope="module")
def accounts_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """REPLICAS access-control stack with the Postgres accounts provider (password
    login, sessions, invites) alongside the redis key provider — so a spec resolves
    ``tai-sess-`` sessions and ``sk-`` keys against one deployment. Seeded with a
    root key like ``auth_stack``."""
    yield from _boot(infra, tmp_path_factory.mktemp("accounts"), build_accounts_stack, seed_auth=True)


@pytest.fixture(scope="module")
def oidc_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory, oauth_idp: OAuthIdp) -> Iterator[TaiStack]:
    """The accounts stack plus the OIDC login provider (``accounts-oidc``) and the
    validate-only OIDC identity provider (``identity-oidc``), both pointed at the
    in-process signing issuer (``oauth_idp``). Seeded with a root key like
    ``auth_stack``; the issuer origin is passed as a resource coordinate."""
    resource_kwargs = {"oidc_issuer_base_url": oauth_idp.base_url}
    yield from _boot(
        infra, tmp_path_factory.mktemp("oidc"), build_oidc_stack, resource_kwargs=resource_kwargs, seed_auth=True
    )


@pytest.fixture(scope="module")
def channel_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    fake_telegram: FakeTelegram,
    fake_slack: FakeSlack,
    fake_twilio: FakeTwilio,
) -> Iterator[TaiStack]:
    """REPLICAS, no backend worker, all three channel plugins loaded — the
    channel cross-worker loop / allowlist / notify / config-error home. Points
    each plugin's outbound API base URL at that medium's in-process stub."""
    resource_kwargs = {
        "telegram_api_base_url": fake_telegram.api_base_url,
        "slack_api_base_url": fake_slack.api_base_url,
        "twilio_api_base_url": fake_twilio.api_base_url,
    }
    yield from _boot(infra, tmp_path_factory.mktemp("channel"), build_channel_stack, resource_kwargs=resource_kwargs)


@pytest.fixture(scope="module")
def bridge_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    llm_stub: LlmStub,
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
) -> Iterator[tuple[TaiStack, str]]:
    """The messaging-bridge profile, yielding ``(stack, root_token)``.

    REPLICAS + backend + metrics, access control ON, the redis conversations backend, the
    memory checkpoint provider, the twilio + whatsapp channel plugins (outbound
    pointed at their in-process stubs), and the ``tools_agent`` + ``deep_agent`` agents on
    the scripted LLM stub. Seeded BEFORE boot with a root ``*`` key and a route table that
    pins the unauthenticated channel webhook doors + the readiness probes public and maps
    every other path to the ``e2e-all`` scope the root satisfies and tests mint against."""
    resource_kwargs = {
        "llm_base_url": llm_stub.base_url,
        "twilio_api_base_url": fake_twilio.api_base_url,
        "whatsapp_api_base_url": fake_whatsapp.api_base_url,
    }
    root = tmp_path_factory.mktemp("bridge")
    resources, config = _allocate_and_build(infra, root, build_bridge_stack, resource_kwargs, False)
    stack = TaiStack(config, infra, resources, root)
    try:
        root_token = seed_bridge_authz(infra, resources)
    except BaseException:
        stack.teardown()
        raise
    # Set the token BEFORE boot: this REPLICAS stack drains the boot reload gate through the
    # MCP probe, which the access-controlled stack fences, so the probe must carry the root token.
    stack.auth_token = root_token
    with stack, diagnostics.track(stack):
        yield stack, root_token


@pytest.fixture(scope="module")
def payments_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    fake_stripe: FakeStripe,
) -> Iterator[tuple[TaiStack, str]]:
    """The Stripe payments profile, yielding ``(stack, root_token)``.

    REPLICAS + access control ON, no backend/metrics, the stripe verifier + the three
    stripe tools + the ask_external extension. The three payment secrets/coordinates ride
    in as resources — the webhook-signing secret the test holds and signs with, the one
    bridge-callback secret both the door and the bridge read, and the FakeStripe origin the
    tools' ``STRIPE_API_BASE`` points at. Seeded BEFORE boot with a root ``*`` key and a
    route table pinning the webhook ingress + callback door public (readiness probes are
    denied until the table pins them public), exactly as ``bridge_stack``."""
    resource_kwargs = {
        "stripe_webhook_secret": secrets.token_hex(16),
        "bridge_callback_secret": secrets.token_hex(16),
        "stripe_stub_base": fake_stripe.api_base_url,
    }
    root = tmp_path_factory.mktemp("payments")
    resources, config = _allocate_and_build(infra, root, build_payments_stack, resource_kwargs, False)
    stack = TaiStack(config, infra, resources, root)
    try:
        root_token = seed_payments_authz(infra, resources)
    except BaseException:
        stack.teardown()
        raise
    # Set the token BEFORE boot: this REPLICAS stack drains the boot reload gate through the
    # MCP probe, which the access-controlled stack fences, so the probe must carry the root token.
    stack.auth_token = root_token
    with stack, diagnostics.track(stack):
        yield stack, root_token


@pytest.fixture(scope="module")
def schedule_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """REPLICAS backend stack carrying the ``schedule_task`` probe branch + the
    backend's scheduler process — the scheduling spec's home, held apart from the
    reload-heavy shared ``replicas`` profile (see ``build_schedule_stack`` for the
    celery prefork-restart limitation that motivates the split)."""
    yield from _boot(infra, tmp_path_factory.mktemp("schedule"), build_schedule_stack)


@pytest.fixture(scope="module")
def agents_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory, llm_stub: LlmStub) -> Iterator[TaiStack]:
    resource_kwargs = {"llm_base_url": llm_stub.base_url}
    yield from _boot(infra, tmp_path_factory.mktemp("agents"), build_agents_stack, resource_kwargs=resource_kwargs)


@pytest.fixture(scope="module")
def agents_redis_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory, llm_stub: LlmStub) -> Iterator[TaiStack]:
    """REPLICAS agents stack on the production-default langgraph ``redis``
    checkpoint/store provider. Gated on the module-capable checkpoint Redis: when
    ``TAI_E2E_CHECKPOINT_REDIS_URL`` is unset the module skips with the compose
    hint (``infra.checkpoint_redis`` is None)."""
    if infra.checkpoint_redis is None:
        pytest.skip(
            "checkpoint Redis not configured; set TAI_E2E_CHECKPOINT_REDIS_URL and start "
            "`docker compose --profile agents-redis up -d`"
        )
    resource_kwargs = {"llm_base_url": llm_stub.base_url}
    yield from _boot(
        infra,
        tmp_path_factory.mktemp("agents-redis"),
        build_agents_redis_stack,
        resource_kwargs=resource_kwargs,
        allocate_checkpoint_db=True,
    )


@pytest.fixture(scope="module")
def connectors_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory, oauth_idp: OAuthIdp) -> Iterator[TaiStack]:
    resource_kwargs = {
        "idp_base_url": oauth_idp.base_url,
        # The connector crypto settings decode these with STANDARD base64
        # (``b64decode(validate=True)``, KEK to exactly 32 bytes), so mint padded
        # standard-base64 keys, not ``token_urlsafe`` which fails that strict decode.
        "connectors_kek": base64.b64encode(secrets.token_bytes(32)).decode(),
        "connectors_state_hmac_key": base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    yield from _boot(
        infra, tmp_path_factory.mktemp("connectors"), build_connectors_stack, resource_kwargs=resource_kwargs
    )


@pytest.fixture(scope="module")
def shipped_connectors_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The stack loading the shipped OAuth connector plugins (google + atlassian).
    Its connector crypto reads the KEK and state-HMAC key as STANDARD base64
    (``b64decode(validate=True)``, KEK to exactly 32 bytes), so mint padded
    standard-base64 keys, not ``token_urlsafe`` which fails that strict decode."""
    resource_kwargs = {
        "connectors_kek": base64.b64encode(secrets.token_bytes(32)).decode(),
        "connectors_state_hmac_key": base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    yield from _boot(
        infra,
        tmp_path_factory.mktemp("shipped-connectors"),
        build_shipped_connectors_stack,
        resource_kwargs=resource_kwargs,
    )


def _seed_postgres_mcp_probe(resources: StackResources) -> None:
    """Create the probe relation + one known row in the stack's Postgres clone. The dynamic
    Postgres MCP child introspects the LIVE schema at startup, so the table must exist BEFORE
    the stack boots (and thus before the mount's child launches) for its CRUD tools to be
    generated and discovered — the seed row is what the round-trip test reads back."""
    table = sql.Identifier(POSTGRES_MCP_PROBE_SCHEMA, POSTGRES_MCP_PROBE_TABLE)
    with (
        psycopg.connect(
            host=resources.pg_host,
            port=resources.pg_port,
            user=resources.pg_user,
            password=resources.pg_password,
            dbname=resources.pg_db,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql.SQL("CREATE TABLE {} (id bigserial PRIMARY KEY, name text NOT NULL)").format(table))
        cur.execute(sql.SQL("INSERT INTO {} (name) VALUES (%s)").format(table), (POSTGRES_MCP_PROBE_ROW_NAME,))
        conn.commit()


@pytest.fixture(scope="module")
def postgres_mcp_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The stack mounting the dynamic Postgres MCP server (``tai42-postgres-mcp``) as a stdio
    child over the manifest ``mcp`` seam, exposing its generated per-table CRUD tools through
    the harness. A known probe table is seeded into the isolated clone BEFORE boot (the child
    introspects the schema at startup), so the mount discovers its tools when the app comes up."""
    root = tmp_path_factory.mktemp("postgres-mcp")
    resources, config = _allocate_and_build(infra, root, build_postgres_mcp_stack, None, False)
    stack = TaiStack(config, infra, resources, root)
    try:
        _seed_postgres_mcp_probe(resources)
    except BaseException:
        stack.teardown()
        raise
    with stack, diagnostics.track(stack):
        yield stack


@pytest.fixture(scope="module")
def extensions_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """MULTIWORKER(1) stack loading the cache/chain/monitor/ask_external tool
    extensions with the fixture monitoring backend — the tool-extension coverage
    home."""
    yield from _boot(infra, tmp_path_factory.mktemp("extensions"), build_extensions_stack)


@pytest.fixture(scope="module")
def monitoring_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    # The deterministic headless-init keys the compose monitoring profile sets.
    resource_kwargs = {
        "langfuse_host": "http://127.0.0.1:3000",
        "langfuse_public_key": "pk-lf-e2e0000000000000000000000000000",
        "langfuse_secret_key": "sk-lf-e2e0000000000000000000000000000",
    }
    yield from _boot(
        infra, tmp_path_factory.mktemp("monitoring"), build_monitoring_stack, resource_kwargs=resource_kwargs
    )


@pytest.fixture(scope="module")
def projection_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The operations-projection profile (auth OFF): a booted stack whose management
    tools come from operation projection, not builtin modules — the default-enabled
    ``api_tools`` surface the projection-chain spec inspects."""
    yield from _boot(infra, tmp_path_factory.mktemp("projection"), build_projection_stack)


@pytest.fixture(scope="module")
def projection_authz_stack(
    infra: Infra, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TaiStack, str, str]]:
    """The projection AUTHZ profile (access control ON), yielding
    ``(stack, root_token, limited_token)``. The route table + the two principals are
    seeded BEFORE boot (readiness probes are denied until the table pins them
    public), exactly as ``boot_stack``'s bootstrap seed runs pre-boot."""
    root = tmp_path_factory.mktemp("projection-authz")
    resources, config = _allocate_and_build(infra, root, build_projection_authz_stack, None, False)
    stack = TaiStack(config, infra, resources, root)
    try:
        root_token, limited_token = seed_projection_authz(infra, resources)
    except BaseException:
        stack.teardown()
        raise
    with stack:
        stack.auth_token = root_token
        with diagnostics.track(stack):
            yield stack, root_token, limited_token


@pytest.fixture(scope="module")
def admin_bypass_authz_stack(
    infra: Infra, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TaiStack, str, str]]:
    """The access-control admin-bypass profile (access control ON), yielding
    ``(stack, admin_token, scoped_token)``. Reuses the projection-authz manifest (auth
    ON, ``/api/tools`` + ``/api/manifest`` mounted) but ``seed_admin_bypass_authz`` seeds
    a DELIBERATELY PARTIAL route table (no catch-all) that leaves ``/api/tools`` unmapped
    — so the middleware's CASE-A super-admin carve-out is exercised on a real route, the
    one path no catch-all-seeding stack can reach. Seeded BEFORE boot (readiness probes
    are denied until the table pins them public), exactly as ``projection_authz_stack``."""
    root = tmp_path_factory.mktemp("admin-bypass-authz")
    resources, config = _allocate_and_build(infra, root, build_projection_authz_stack, None, False)
    stack = TaiStack(config, infra, resources, root)
    try:
        admin_token, scoped_token = seed_admin_bypass_authz(infra, resources)
    except BaseException:
        stack.teardown()
        raise
    with stack:
        stack.auth_token = admin_token
        with diagnostics.track(stack):
            yield stack, admin_token, scoped_token
