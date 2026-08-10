"""Reusable stack orchestration: infra connect, per-stack resource allocation,
and the access-control bootstrap seed.

These live in the installable package (not in ``tests/conftest.py``) so BOTH the
pytest suite and the standalone ``tai42-e2e-studio-stack`` console runner drive one
implementation. The pytest fixtures wrap these; the runner calls them directly.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from pathlib import Path

import psycopg

from tai42_e2e.pg import PostgresAdmin
from tai42_e2e.redisx import RedisAdmin
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import Infra, InfraUnavailable, StackResources
from tai42_e2e.variants import resolve_variants


def connect_infra(settings: HarnessSettings) -> Infra:
    """Resolve the variant set, connect the Redis + Postgres admin clients,
    verify everything the selected variants need is reachable (loudly, with the
    compose hint on failure), apply the DDL template, and return the
    :class:`Infra` bundle. The caller owns closing ``infra.redis``."""
    variants = resolve_variants(settings)
    host, port = settings.redis_host_port
    redis_admin = RedisAdmin(host, port)
    pg_admin = PostgresAdmin(settings)
    try:
        redis_admin.check_reachable()
    except Exception as exc:
        raise InfraUnavailable(f"Redis not usable ({exc}). Start it with `docker compose up -d`.") from exc
    try:
        pg_admin.check_reachable()
    except Exception as exc:
        raise InfraUnavailable(f"Postgres not reachable ({exc}). Start it with `docker compose up -d`.") from exc
    # Extra reachability the backend needs beyond the shared Redis + Postgres
    # (celery's broker); a no-op for the Redis-only backends.
    variants.backend.infra_check(settings)
    # The module-capable checkpoint Redis is optional (present only when its URL is set);
    # when set it must be reachable, failing loudly like the main Redis. Its allocator owns
    # DB 0 ONLY: RediSearch refuses indexes on db != 0 and index names are server-global,
    # so a stack on DB 1 would silently read another stack's index. A one-slot pool makes a
    # second concurrent checkpoint stack fail loudly instead of mis-indexing; FLUSHDB at
    # allocation drops the indexes so successive stacks reuse DB 0 cleanly.
    checkpoint_redis: RedisAdmin | None = None
    if settings.checkpoint_redis_url is not None:
        ck_host, ck_port = settings.checkpoint_redis_host_port
        checkpoint_redis = RedisAdmin(ck_host, ck_port, stack_db_range=range(1))
        try:
            checkpoint_redis.check_reachable()
        except Exception as exc:
            raise InfraUnavailable(
                f"Checkpoint Redis not reachable ({exc}). Start it with `docker compose --profile agents-redis up -d`."
            ) from exc
        try:
            # The checkpoint Redis exists only for its modules; a module-free image
            # here is a misconfiguration, caught loudly at session start.
            checkpoint_redis.check_search_json_modules()
        except Exception as exc:
            raise InfraUnavailable(
                f"Checkpoint Redis lacks the RediSearch/RedisJSON modules the langgraph redis provider needs "
                f"({exc}). Use a module-capable image (redis:8); `docker compose --profile agents-redis up -d`."
            ) from exc
    pg_admin.ensure_template()
    return Infra(
        settings=settings, redis=redis_admin, pg=pg_admin, variants=variants, checkpoint_redis=checkpoint_redis
    )


def allocate_resources(
    infra: Infra,
    root: Path,
    *,
    allocate_checkpoint_db: bool = False,
    redis_host: str | None = None,
    redis_port: int | None = None,
    bus_redis_host: str | None = None,
    bus_redis_port: int | None = None,
    pg_host: str | None = None,
    pg_port: int | None = None,
    **extra: object,
) -> StackResources:
    """Reserve a Redis logical DB, a per-stack Postgres database clone, and (for
    a broker-bearing backend) a per-stack broker lease, then pack the coordinates
    a manifest/env builder needs.

    ``allocate_checkpoint_db`` additionally reserves a logical DB on the
    module-capable checkpoint Redis (the langgraph redis checkpoint/store leg);
    it requires the checkpoint Redis to be configured (``connect_infra`` built
    it), raising loudly otherwise so a mis-gated fixture never silently runs the
    memory provider.

    ``redis_host``/``redis_port`` and ``pg_host``/``pg_port`` point the SUT at
    DIFFERENT connection ENDPOINTS for the stores the harness still allocates and
    seeds on the real infra: the logical Redis DB and the Postgres database clone
    are created as always, and the returned resources simply reach them through
    the given endpoints — the infra-outage specs point them at a per-stack TCP
    relay they can sever. The Redis override is an endpoint, not a URL, because
    the logical DB index is chosen here: the returned ``redis_url`` always selects
    the index this call reserved, so ``redis_url`` and ``redis_idx`` can never
    disagree. ``probe_redis_url`` is deliberately NOT rerouted — it stays on the
    real Redis so the harness can still read probe records while the SUT's own
    endpoint is severed.

    ``bus_redis_host``/``bus_redis_port`` reroute the app worker bus through its OWN
    endpoint, INDEPENDENT of the feature-Redis one, so a bus-outage spec severs the
    bus (its own relay) without severing auth/feature stores. Unset, the bus rides
    the real Redis directly. The returned ``bus_redis_url`` selects the stack's own
    logical DB (presence keys flush with the DB on release); ``bus_namespace`` is
    unique per stack because bus pub/sub + presence keys are server-global and the
    logical-DB isolation does NOT isolate them."""
    # No TaiStack exists yet to reap these on failure, so if a later step raises, release
    # each already-acquired resource here — a partial allocation must not leak. Each cleanup
    # runs only for a resource actually acquired.
    if allocate_checkpoint_db and infra.checkpoint_redis is None:
        raise RuntimeError(
            "allocate_checkpoint_db=True but the checkpoint Redis is not configured "
            "(set TAI_E2E_CHECKPOINT_REDIS_URL and start `docker compose --profile agents-redis up -d`)"
        )
    idx = infra.redis.allocate_db()
    checkpoint_idx: int | None = None
    lease = None
    dbname: str | None = None
    try:
        if allocate_checkpoint_db:
            assert infra.checkpoint_redis is not None
            checkpoint_idx = infra.checkpoint_redis.allocate_db()
        stack_id = uuid.uuid4().hex[:6]
        dbname = f"tai42_e2e_{stack_id}"
        infra.pg.create_stack_db(dbname)
        storage_root = root / "storage"
        storage_root.mkdir(parents=True, exist_ok=True)
        lease = infra.variants.backend.allocate_broker(infra, stack_id)
        settings = infra.settings
        checkpoint_url = (
            infra.checkpoint_redis.url_for(checkpoint_idx)
            if checkpoint_idx is not None and infra.checkpoint_redis is not None
            else None
        )
        infra_redis_host, infra_redis_port = settings.redis_host_port
        sut_redis_host = redis_host if redis_host is not None else infra_redis_host
        sut_redis_port = redis_port if redis_port is not None else infra_redis_port
        sut_bus_host = bus_redis_host if bus_redis_host is not None else infra_redis_host
        sut_bus_port = bus_redis_port if bus_redis_port is not None else infra_redis_port
        # The bus namespace reuses the per-stack id (``dbname`` is ``tai42_e2e_<id>``):
        # a single unique-per-stack token isolates the server-global bus channels.
        # Inside the reap guard: an unknown key in ``extra`` raises TypeError here, and
        # that must not strand the DB clone, the vhost lease and the logical indices.
        return StackResources(
            redis_idx=idx,
            redis_url=f"redis://{sut_redis_host}:{sut_redis_port}/{idx}",
            probe_redis_url=infra.redis.url_for(0),
            bus_redis_url=f"redis://{sut_bus_host}:{sut_bus_port}/{idx}",
            bus_namespace=dbname,
            pg_host=pg_host if pg_host is not None else settings.pg_host,
            pg_port=pg_port if pg_port is not None else settings.pg_port,
            pg_user=settings.pg_user,
            pg_password=settings.pg_password,
            pg_db=dbname,
            storage_root=str(storage_root),
            broker_url=lease.broker_url if lease is not None else None,
            broker_lease=lease,
            checkpoint_redis_idx=checkpoint_idx,
            checkpoint_redis_url=checkpoint_url,
            **extra,  # type: ignore[arg-type]
        )
    except BaseException:
        if lease is not None:
            lease.release()
        if dbname is not None:
            infra.pg.drop_stack_db(dbname)
        if checkpoint_idx is not None and infra.checkpoint_redis is not None:
            infra.checkpoint_redis.release_db(checkpoint_idx)
        infra.redis.release_db(idx)
        raise


def release_resources(infra: Infra, resources: StackResources) -> None:
    """Reap an allocation that no :class:`TaiStack` owns yet — the mirror of
    ``TaiStack.teardown``'s resource release, for a failure between
    ``allocate_resources`` and the stack that would otherwise reap it. Every resource
    is released even if one release raises (a flaky vhost delete must not strand the
    Postgres clone and the Redis index); failures are collected and raised together so
    they surface without displacing the error that led here (they chain under it)."""
    errors: list[str] = []
    if resources.broker_lease is not None:
        try:
            resources.broker_lease.release()
        except Exception as exc:
            errors.append(f"release broker lease: {exc!r}")
    try:
        infra.pg.drop_stack_db(resources.pg_db)
    except Exception as exc:
        errors.append(f"drop stack db {resources.pg_db}: {exc!r}")
    if resources.checkpoint_redis_idx is not None:
        if infra.checkpoint_redis is None:
            errors.append("resources hold a checkpoint Redis DB but infra.checkpoint_redis is None (leak)")
        else:
            infra.checkpoint_redis.release_db(resources.checkpoint_redis_idx)
    infra.redis.release_db(resources.redis_idx)
    if errors:
        raise RuntimeError("releasing an unowned allocation found leaks:\n  " + "\n  ".join(errors))


# ---- access-control bootstrap seed --------------------------------------


def seed_root_identity(
    infra: Infra,
    resources: StackResources,
    *,
    user_id: str,
    scopes: Sequence[str],
    raw: str | None = None,
) -> str:
    """Seed ONE key across the two storage homes the mint orchestration writes:
    the identity provider's private record (delegated to the identity variant,
    since that wire format is the provider's storage, not a harness contract) and
    a policy row in the Postgres policy store. ``raw`` pins the token (the
    browser-e2e runner needs a known key up front); left unset it is minted.
    Returns the raw ``sk-...`` token the caller authenticates with."""
    raw = raw if raw is not None else f"sk-{secrets.token_urlsafe(32)}"
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    scope_list = list(scopes)

    infra.variants.identity.seed_identity(infra, resources, user_id=user_id, hashed=hashed)

    with psycopg.connect(
        host=resources.pg_host,
        port=resources.pg_port,
        user=resources.pg_user,
        password=resources.pg_password,
        dbname=resources.pg_db,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO access_control_policies (user_id, scopes) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET scopes = EXCLUDED.scopes",
                (user_id, scope_list),
            )
        conn.commit()
    return raw


def seed_route_rows(resources: StackResources, rows: Sequence[tuple[str, str, str | None]]) -> None:
    """Upsert ``(url, scope_id, pattern)`` rows into the Postgres route store the
    access-control verifier reads. ``pattern`` is a regex for a dynamic mapping or
    ``None`` for an exact-path mapping."""
    with psycopg.connect(
        host=resources.pg_host,
        port=resources.pg_port,
        user=resources.pg_user,
        password=resources.pg_password,
        dbname=resources.pg_db,
    ) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO access_control_routes (url, scope_id, pattern) VALUES (%s, %s, %s) "
                "ON CONFLICT (url) DO UPDATE SET scope_id = EXCLUDED.scope_id, pattern = EXCLUDED.pattern",
                list(rows),
            )
        conn.commit()


# The two-tier route mapping the browser-e2e studio stack runs. Tier one
# (``ACCESS_CONTROL_PATH_PATTERNS``): a request-path regex names a route TEMPLATE. Tier two
# (the PG route store seeded by ``seed_studio_auth``): each template resolves to a resource
# id. ``studio_authed`` (every other ``/api`` route) carries a negative lookahead excluding
# the two public ``/api`` shapes so the registry listing stays authed.
STUDIO_PATH_PATTERNS: dict[str, str] = {
    r"/api/(?!plugins/[^/]+/studio/)(?!interactions/callback(?:/|$)).*": "studio_authed",
    r"/(?!api(?:/|$)).*": "public_spa",
    r"/api/plugins/[^/]+/studio/.*": "public_assets",
    r"/api/interactions/callback(?:/.*)?": "public_callback",
}

# The single protected resource id the Studio's ``*``-scope key is authorized
# for; the blanket authed-``/api`` pattern resolves to it.
STUDIO_RESOURCE_ID = "studio"


def seed_studio_auth(infra: Infra, resources: StackResources, *, api_key: str) -> str:
    """Seed the browser-e2e studio stack's auth: a ``*``-scope root key pinned to
    ``api_key`` (the value Playwright pastes at ``/login``), and the tier-two
    route→resource table the ``STUDIO_PATH_PATTERNS`` templates resolve through —
    ``studio_authed`` → the ``studio`` resource, the public SPA/asset/callback
    templates → the public marker, and the readiness probes pinned public.
    Returns the raw root token."""
    raw = seed_root_identity(infra, resources, user_id="studio-root", scopes=["*"], raw=api_key)
    seed_route_rows(
        resources,
        [
            ("/health", "public", None),
            ("/metrics", "public", None),
            ("/ready", "public", None),
            ("studio_authed", STUDIO_RESOURCE_ID, None),
            ("public_spa", "public", None),
            ("public_assets", "public", None),
            ("public_callback", "public", None),
        ],
    )
    return raw


def seed_bootstrap_key(infra: Infra, resources: StackResources) -> str:
    """Seed the root ``*``-scope key plus a catch-all route→scope table so every
    route resolves to a scope the root satisfies (enforcement denies any route
    with no scope mapping) and the ``e2e-all`` scope is mintable for keys the
    tests provision. Returns the raw root token."""
    raw = seed_root_identity(infra, resources, user_id="e2e-root", scopes=["*"])
    # Enforcement denies any route with no scope mapping, so seed the route table: pin the
    # readiness probes (/health, /metrics) public, and map every other path to "e2e-all"
    # via a negative-lookahead pattern that never captures the two public probes (deny-wins
    # would otherwise re-protect them).
    seed_route_rows(
        resources,
        [
            ("/health", "public", None),
            ("/metrics", "public", None),
            ("e2e-all-routes", "e2e-all", r"^/(?!health$)(?!metrics$).*$"),
        ],
    )
    return raw


def seed_bridge_authz(infra: Infra, resources: StackResources) -> str:
    """Seed the messaging-bridge stack's auth before boot: a root ``*``-scope key plus a
    route table that pins the unauthenticated channel webhook doors public.

    The channel inbound/status doors carry their OWN signature auth (Twilio HMAC, Meta
    X-Hub-Signature-256), so they must resolve to ``public`` or the access-control guard
    would 403 them before the plugin's signature check runs. Every other path maps to the
    ``e2e-all`` scope the root satisfies and the tests mint keys against; the readiness
    probes stay public so boot's readiness wait is not itself denied. Returns the raw root
    token."""
    raw = seed_root_identity(infra, resources, user_id="bridge-root", scopes=["*"])
    seed_route_rows(
        resources,
        [
            ("/health", "public", None),
            ("/metrics", "public", None),
            ("/ready", "public", None),
            # Every channel webhook door (twilio inbound/status, whatsapp inbound) and the web
            # channel's PUBLIC chat doors: unauthenticated at the platform edge, authenticated
            # by the provider signature or the visitor's session cookie.
            ("bridge-channels", "public", r"^/api/channels/.*$"),
            # The web entry-gate MANAGEMENT doors are authed (platform api key): pin them to
            # e2e-all so the blanket public channel rule above does not open them. Deny wins
            # across tiers, so the path resolving to BOTH ids stays protected — an unauthed
            # caller is refused, the operator (root) is admitted.
            ("bridge-web-gates", "e2e-all", r"^/api/channels/web/gates(?:/.*)?$"),
            # The interactions callback door a channel adapter forwards an ask_user reply
            # to (a server-side loopback POST that carries no token).
            ("bridge-callback", "public", r"^/api/interactions/callback(?:/.*)?$"),
            # Everything else (the authed conversation-route CRUD, the message door, key
            # mint, schedules) → e2e-all, excluding the public shapes so deny-wins never
            # re-protects them.
            (
                "bridge-protected",
                "e2e-all",
                r"^/(?!health$)(?!metrics$)(?!ready$)(?!api/channels/)(?!api/interactions/callback).*$",
            ),
        ],
    )
    return raw


def seed_payments_authz(infra: Infra, resources: StackResources) -> str:
    """Seed the Stripe payments stack's auth before boot: a root ``*``-scope key plus a
    route table that pins the two unauthenticated payment doors public.

    Both doors are unauthenticated by nature — the Stripe webhook ingress carries no
    platform API key (the topic's ``stripe`` verifier checks its signature) and the bridge
    callback door authenticates with the question's own ``X-TAI-Bridge-Secret`` verifier —
    so both must resolve to ``public`` or the access-control guard would 403 them before the
    signature/header check runs. Every other path — ``/mcp`` included — maps to the
    ``e2e-all`` scope the root satisfies; the readiness probes stay public so boot's
    readiness wait is not itself denied. Every public shape is excluded from the protected
    catch-all so deny-wins never re-protects it. Returns the raw root token."""
    raw = seed_root_identity(infra, resources, user_id="payments-root", scopes=["*"])
    seed_route_rows(
        resources,
        [
            ("/health", "public", None),
            ("/metrics", "public", None),
            ("/ready", "public", None),
            # The Stripe webhook ingress: unauthenticated at the platform edge, authenticated
            # by the topic's stripe-signature verifier.
            ("payments-webhook", "public", r"^/universal_webhook/.*$"),
            # The interaction callback door the bridge POSTs the answer to (its own
            # X-TAI-Bridge-Secret verifier is the authentication).
            ("payments-callback", "public", r"^/api/interactions/callback(?:/.*)?$"),
            # Everything else (the verifier bind, hook register, preset create, the MCP edge)
            # → e2e-all, excluding the public shapes so deny-wins never re-protects them.
            (
                "payments-protected",
                "e2e-all",
                r"^/(?!health$)(?!metrics$)(?!ready$)(?!universal_webhook/)(?!api/interactions/callback).*$",
            ),
        ],
    )
    return raw


def seed_projection_authz(infra: Infra, resources: StackResources) -> tuple[str, str]:
    """Seed the projection-authz stack's two principals + route table, before boot.

    Returns ``(root_token, limited_token)``. ``root`` carries the ``*`` scope; the
    ``limited`` key carries ONLY ``mcp-access``. The route table pins ``/mcp`` to the
    ``mcp-access`` scope (so the limited key's bearer PASSES the HTTP guard and
    reaches the MCP tool edge) while every projected operation's own synthesized
    route (e.g. ``/api/config/reload``) maps to ``e2e-all`` — which the limited key
    LACKS. So the limited key authenticates and dispatches, then the tool-edge authz
    check denies the specific projected op with a ``PermissionDenied``-backed
    ``ToolError``; the root key's ``*`` satisfies both and is allowed. The readiness
    probes stay public so boot's readiness wait is not itself denied."""
    root = seed_root_identity(infra, resources, user_id="proj-root", scopes=["*"])
    limited = seed_root_identity(infra, resources, user_id="proj-limited", scopes=["mcp-access"])
    seed_route_rows(
        resources,
        [
            ("/health", "public", None),
            ("/metrics", "public", None),
            ("/ready", "public", None),
            # The MCP transport endpoint itself: reachable by a key carrying
            # ``mcp-access`` (the limited key) or ``*`` (root). The exact row wins for
            # ``/mcp``; the protected pattern below excludes it so ``/mcp`` resolves to
            # this scope alone (not also ``e2e-all``, which deny-wins would apply).
            ("/mcp", "mcp-access", None),
            # Every OTHER path (a projected op's synthesized route included) → e2e-all.
            ("proj-protected", "e2e-all", r"^/(?!health$)(?!metrics$)(?!ready$)(?!mcp$).*$"),
        ],
    )
    return root, limited


def seed_admin_bypass_authz(infra: Infra, resources: StackResources) -> tuple[str, str]:
    """Seed the admin-bypass stack's two principals + a DELIBERATELY PARTIAL route table.

    Returns ``(admin_token, scoped_token)``. ``admin`` carries the condition-free ``*``
    policy that is not an owned key — the SUPER-ADMIN discriminator (``is_admin=True``)
    the ``ResourceGuardMiddleware`` admits on an unmapped route. ``scoped`` carries ONLY
    ``bypass-probe`` (a non-``*`` scope), so it is never admin.

    Unlike the other auth seeds, this table has NO catch-all pattern: it pins the readiness
    probes public and maps one real route (``/api/manifest``) to ``bypass-probe``, but
    deliberately leaves ``/api/tools`` (a real authenticated GET) with no row. So
    ``/api/tools`` resolves to no resource and hits the middleware's CASE A: the admin
    discriminator is admitted by the super-admin carve-out, while the scoped key is denied
    ``Forbidden: Route not configured``. A catch-all would hide CASE A, so it is omitted."""
    admin = seed_root_identity(infra, resources, user_id="bypass-admin", scopes=["*"])
    scoped = seed_root_identity(infra, resources, user_id="bypass-scoped", scopes=["bypass-probe"])
    seed_route_rows(
        resources,
        [
            ("/health", "public", None),
            ("/metrics", "public", None),
            ("/ready", "public", None),
            # The one explicitly-mapped real route the scoped key is authorized for, so a
            # non-admin denial on the UNMAPPED /api/tools is provably CASE A (not-configured),
            # not a blanket scope denial. A pure in-process read, needs no backend worker.
            ("/api/manifest", "bypass-probe", None),
        ],
    )
    return admin, scoped
