"""Access-control seeding: the owner principal and its keys in the setup-door shape, and
the per-stack route -> scope tables the seeded root authorizes against.

The stack is not up when these run (readiness itself needs a token), and the browser-e2e
runner needs a PINNED raw key, so the setup door cannot be the seed path; these write the
rows the setup door plus a mint would produce directly into the stores. They live in the
installable package (not in ``tests/conftest.py``) so BOTH the pytest suite and the
standalone ``tai42-e2e-studio-stack`` console runner drive one implementation.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Sequence

import psycopg
from psycopg.types.json import Json
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM

from tai42_e2e.topology import Infra, StackResources


def seed_owner_and_key(
    infra: Infra,
    resources: StackResources,
    *,
    owner_id: str,
    key_id: str,
    scopes: Sequence[str],
    raw: str | None = None,
) -> str:
    """Seed an owner principal and its first key exactly as the setup door mints them.

    The stack is not up yet (readiness itself needs the token) and the browser-e2e
    runner needs a PINNED raw key, so the setup door cannot be the seed path; instead
    this writes the four rows the setup door + a mint produce directly into the stores:

    1. the owner principal (``kind='human'``, ``created_by=NULL``) — the top-level
       principal every key belongs to;
    2. the owner's own admin policy (``scopes=['*']``, no condition, no role pointer),
       the shape ``roles.apply_role(owner, 'admin')`` writes for the allow_all admin role;
    3. the key's policy row carrying the two server-owned claims a mint stamps into
       ``policy_data`` — a fresh key fingerprint and the owner claim;
    4. the key's identity record with the owner claim (delegated to the identity
       variant, whose wire format is the provider's own storage).

    ``raw`` pins the token; left unset it is minted. Returns the raw ``sk-...`` token
    the caller authenticates with. The seeded key is a full admin: its ``*`` scopes
    intersect the owner's ``*`` with no condition on either side. Calling this twice with
    the same ``owner_id`` and different ``key_id`` gives one owner several keys.
    """
    raw = raw if raw is not None else f"sk-{secrets.token_urlsafe(32)}"
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    scope_list = list(scopes)

    infra.variants.identity.seed_identity(infra, resources, user_id=key_id, hashed=hashed, owner_user_id=owner_id)

    key_policy_data = {KEY_FINGERPRINT_CLAIM: uuid.uuid4().hex, OWNER_USER_ID_CLAIM: owner_id}
    with psycopg.connect(
        host=resources.pg_host,
        port=resources.pg_port,
        user=resources.pg_user,
        password=resources.pg_password,
        dbname=resources.pg_db,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO access_control_principals (user_id, kind, display_name, created_by) "
                "VALUES (%s, 'human', %s, NULL) ON CONFLICT (user_id) DO NOTHING",
                (owner_id, owner_id),
            )
            cur.execute(
                "INSERT INTO access_control_policies (user_id, scopes) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET scopes = EXCLUDED.scopes",
                (owner_id, ["*"]),
            )
            cur.execute(
                "INSERT INTO access_control_policies (user_id, scopes, policy_data) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET scopes = EXCLUDED.scopes, policy_data = EXCLUDED.policy_data",
                (key_id, scope_list, Json(key_policy_data)),
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
# the plugin studio-asset door so the registry listing stays authed. The interactions
# callback and served-media doors are ``authed=False`` capability urls — the verifier's
# declared-public tier resolves them public straight from the route registration, so they
# need NO carve-out here (the ``studio_authed`` catch-all may cover them; the
# declared-public tier short-circuits above the route table).
#
# Dropping the explicit callback/media entries from this published map (they moved to the
# declared-public tier) is a BREAKING change to this exported constant, so it ships as a major.
STUDIO_PATH_PATTERNS: dict[str, str] = {
    r"/api/(?!plugins/[^/]+/studio/).*": "studio_authed",
    r"/(?!api(?:/|$)).*": "public_spa",
    r"/api/plugins/[^/]+/studio/.*": "public_assets",
}

# The single protected resource id the Studio's ``*``-scope key is authorized
# for; the blanket authed-``/api`` pattern resolves to it.
STUDIO_RESOURCE_ID = "studio"


def seed_studio_routes(resources: StackResources) -> None:
    """Seed the browser-e2e studio stack's tier-two route→resource table the
    ``STUDIO_PATH_PATTERNS`` templates resolve through — ``studio_authed`` → the ``studio``
    resource, the public SPA/asset templates → the public marker, and the readiness probes
    pinned public. The interactions callback and served-media doors need no row: the
    verifier's declared-public tier publics them from their ``authed=False`` registration.

    The unseeded setup stack seeds only these rows (no owner or key): readiness's ``/health``
    probe and the public ``/login`` + ``/api/setup`` doors then answer while ``needs_setup``
    stays true, and the owner's key minted through the setup door authorizes ``studio_authed``
    once it exists."""
    seed_route_rows(
        resources,
        [
            ("/health", "public", None),
            ("/metrics", "public", None),
            ("/ready", "public", None),
            ("studio_authed", STUDIO_RESOURCE_ID, None),
            ("public_spa", "public", None),
            ("public_assets", "public", None),
        ],
    )


def seed_studio_auth(infra: Infra, resources: StackResources, *, api_key: str) -> str:
    """Seed the browser-e2e studio stack's auth: a ``*``-scope root key pinned to
    ``api_key`` (the value Playwright pastes at ``/login``), and the tier-two
    route→resource table (:func:`seed_studio_routes`). Returns the raw root token."""
    raw = seed_owner_and_key(infra, resources, owner_id="studio-owner", key_id="studio-root", scopes=["*"], raw=api_key)
    seed_studio_routes(resources)
    return raw


def seed_bootstrap_key(infra: Infra, resources: StackResources) -> str:
    """Seed the ``e2e-owner`` principal and its root ``e2e-root`` ``*``-scope key plus a
    catch-all route→scope table so every route resolves to a scope the root satisfies
    (enforcement denies any route with no scope mapping) and the ``e2e-all`` scope is
    mintable for keys the tests provision. Returns the raw root token."""
    raw = seed_owner_and_key(infra, resources, owner_id="e2e-owner", key_id="e2e-root", scopes=["*"])
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
    raw = seed_owner_and_key(infra, resources, owner_id="bridge-owner", key_id="bridge-root", scopes=["*"])
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
            # The interactions callback and served-media doors need no row: both register
            # ``authed=False``, so the verifier's declared-public tier publics them straight
            # from the route registration (the protected catch-all below may cover them; the
            # declared-public tier short-circuits above the route table).
            # Everything else (the authed conversation-route CRUD, the message door, key
            # mint, schedules) → e2e-all, excluding the public channel shapes so deny-wins
            # never re-protects them.
            (
                "bridge-protected",
                "e2e-all",
                r"^/(?!health$)(?!metrics$)(?!ready$)(?!api/channels/).*$",
            ),
        ],
    )
    return raw


def seed_stripe_authz(infra: Infra, resources: StackResources) -> str:
    """Seed the Stripe payments stack's auth before boot: a root ``*``-scope key plus a
    route table that pins the unauthenticated payment doors public.

    The Stripe webhook ingress is unauthenticated by nature — it carries no platform API
    key (the topic's ``stripe`` verifier checks its signature) — so it must resolve to
    ``public`` or the access-control guard would 403 it before the signature check runs. It
    is a non-/api door, so it needs this row; the interactions callback and served-media
    doors are ``authed=False`` /api routes the verifier's declared-public tier publics from
    their registration, so they need none. Every other path — ``/mcp`` included — maps to the
    ``e2e-all`` scope the root satisfies; the readiness probes stay public so boot's
    readiness wait is not itself denied. The webhook shape is excluded from the protected
    catch-all so deny-wins never re-protects it. Returns the raw root token."""
    raw = seed_owner_and_key(infra, resources, owner_id="stripe-owner", key_id="stripe-root", scopes=["*"])
    seed_route_rows(
        resources,
        [
            ("/health", "public", None),
            ("/metrics", "public", None),
            ("/ready", "public", None),
            # The Stripe webhook ingress: unauthenticated at the platform edge, authenticated
            # by the topic's stripe-signature verifier. A non-/api door, so the verifier's
            # declared-public tier does not reach it — it needs this public row.
            ("stripe-webhook", "public", r"^/universal_webhook/.*$"),
            # The interactions callback and served-media doors need no row: both register
            # ``authed=False``, so the verifier's declared-public tier publics them straight
            # from the route registration (the protected catch-all below may cover them; the
            # declared-public tier short-circuits above the route table).
            # Everything else (the verifier bind, hook register, preset create, the MCP edge)
            # → e2e-all, excluding the webhook shape so deny-wins never re-protects it.
            (
                "stripe-protected",
                "e2e-all",
                r"^/(?!health$)(?!metrics$)(?!ready$)(?!universal_webhook/).*$",
            ),
        ],
    )
    return raw


def seed_projection_authz(infra: Infra, resources: StackResources) -> tuple[str, str]:
    """Seed the projection-authz stack's owner principal, its two keys + route table, before boot.

    Returns ``(root_token, limited_token)`` — both keys of the ``proj-owner`` admin principal.
    ``root`` carries the ``*`` scope; the
    ``limited`` key carries ONLY ``mcp-access``. The route table pins ``/mcp`` to the
    ``mcp-access`` scope (so the limited key's bearer PASSES the HTTP guard and
    reaches the MCP tool edge) while every projected operation's own synthesized
    route (e.g. ``/api/config/reload``) maps to ``e2e-all`` — which the limited key
    LACKS. So the limited key authenticates and dispatches, then the tool-edge authz
    check denies the specific projected op with a ``PermissionDeniedError``-backed
    ``ToolError``; the root key's ``*`` satisfies both and is allowed. The readiness
    probes stay public so boot's readiness wait is not itself denied."""
    root = seed_owner_and_key(infra, resources, owner_id="proj-owner", key_id="proj-root", scopes=["*"])
    limited = seed_owner_and_key(infra, resources, owner_id="proj-owner", key_id="proj-limited", scopes=["mcp-access"])
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
    """Seed the admin-bypass stack's owner principal, its two keys + a DELIBERATELY PARTIAL route table.

    Returns ``(admin_token, scoped_token)`` — both keys of the ``bypass-owner`` admin
    principal. ``admin`` carries the condition-free ``*`` policy under its ``*`` owner, so it
    is the SUPER-ADMIN discriminator (``is_admin=True``) the ``ResourceGuardMiddleware``
    admits on an unmapped route. ``scoped`` carries ONLY ``bypass-probe`` (a non-``*`` scope),
    so it is never admin.

    Unlike the other auth seeds, this table has NO catch-all pattern: it pins the readiness
    probes public and maps one real route (``/api/manifest``) to ``bypass-probe``, but
    deliberately leaves ``/api/tools`` (a real authenticated GET) with no row. So
    ``/api/tools`` resolves to no resource and hits the middleware's CASE A: the admin
    discriminator is admitted by the super-admin carve-out, while the scoped key is denied
    ``Forbidden: Route not configured``. A catch-all would hide CASE A, so it is omitted."""
    admin = seed_owner_and_key(infra, resources, owner_id="bypass-owner", key_id="bypass-admin", scopes=["*"])
    scoped = seed_owner_and_key(
        infra, resources, owner_id="bypass-owner", key_id="bypass-scoped", scopes=["bypass-probe"]
    )
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
