"""Frozen descriptions of a stack — its shape, rendered manifest, feature env,
per-stack resources — plus the shared session-scoped infra and its admin clients.

:class:`StackConfig` profiles live in :mod:`tai42_e2e.manifests`; a booted
:class:`~tai42_e2e.stack.TaiStack` consumes a config + :class:`Infra` +
:class:`StackResources`."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from tai42_e2e.pg import PostgresAdmin
    from tai42_e2e.redisx import RedisAdmin
    from tai42_e2e.settings import HarnessSettings
    from tai42_e2e.variants import BrokerLease, Variants


class InfraUnavailableError(RuntimeError):
    """The shared infra (Redis / Postgres / a backend's broker) could not be
    reached, or a variant selection is unknown; carries the compose hint. Raised
    at session start so a misconfiguration fails loudly, never cryptically
    mid-suite."""


class Topology(enum.Enum):
    """How the ``tai serve`` fleet is shaped.

    ``MULTIWORKER`` is one master with ``--workers N`` on one port (shared
    run-id + mmap dir) — the model the metrics round-trip and import-order
    probes need. ``REPLICAS`` is two ``--workers 1`` masters on two ports
    (shared config/Redis/PG, per-replica metrics dirs) — deterministic A/B
    addressing for every cross-worker and Redis-contention test."""

    MULTIWORKER = "multiworker"
    REPLICAS = "replicas"


@dataclass(frozen=True)
class Infra:
    """Shared, session-scoped services and their admin clients, plus the one
    variant set this process runs under (resolved once at ``connect_infra``)."""

    settings: HarnessSettings
    redis: RedisAdmin
    pg: PostgresAdmin
    variants: Variants
    # The module-capable checkpoint Redis admin (RediSearch + RedisJSON), present
    # only when ``TAI_E2E_CHECKPOINT_REDIS_URL`` is set. The langgraph redis
    # checkpoint/store agents leg allocates its own logical DB here; ``None`` means
    # that leg's stacks skip.
    checkpoint_redis: RedisAdmin | None = None


@dataclass(frozen=True)
class StackResources:
    """The per-stack coordinates a manifest/env builder needs: the allocated
    Redis logical DB, the Postgres database name, the on-disk storage root, and
    optional harness-server URLs the SUT points at."""

    redis_idx: int
    redis_url: str
    probe_redis_url: str
    pg_host: str
    pg_port: int
    pg_user: str
    pg_password: str
    pg_db: str
    storage_root: str
    # The app-owned worker bus coordinates. The bus Redis is reached through its
    # OWN endpoint (independent of ``redis_url``, so a bus-outage scenario can sever
    # the bus without severing auth/feature stores), and the namespace is unique
    # per stack (bus pub/sub channels + presence keys are server-global — the
    # per-stack logical-db isolation does NOT isolate them). Always filled by
    # ``allocate_resources``; the empty defaults exist only for the manifest-render
    # sentinels that never boot a bus.
    bus_redis_url: str = ""
    bus_namespace: str = ""
    # The per-stack broker: the celery variant's isolated vhost AMQP URL and the
    # lease that reaps it in teardown. ``None`` for backends that ride on Redis.
    broker_url: str | None = None
    broker_lease: BrokerLease | None = None
    # The per-stack logical DB on the module-capable checkpoint Redis (the
    # langgraph redis checkpoint/store provider's home). ``None`` on every stack
    # that runs the in-process ``memory`` provider instead.
    checkpoint_redis_idx: int | None = None
    checkpoint_redis_url: str | None = None
    llm_base_url: str | None = None
    gh_webhook_secret: str | None = None
    # The Stripe integration profile's three coordinates. ``stripe_webhook_secret`` is the
    # HMAC secret the topic's ``stripe`` verifier reads and the test signs deliveries with
    # (held test-side, so it cannot be minted inside the manifest builder the way a
    # channel verify-token is). ``bridge_callback_secret`` is the one value BOTH the
    # callback door's ``shared_secret`` verifier and the bridge tool read. ``stripe_stub_base``
    # is the in-process ``FakeStripe`` origin the tools' ``STRIPE_API_BASE`` points at — a
    # resource because the stub's port is allocated at fixture time and the builder only
    # reads it (the two-fixture wiring ``channel_stack`` uses for its provider stubs).
    stripe_webhook_secret: str | None = None
    bridge_callback_secret: str | None = None
    stripe_stub_base: str | None = None
    # The built Studio dist the skeleton serves (STUDIO_DIST_PATH) for the
    # browser-e2e profile.
    studio_dist_path: str | None = None
    connectors_kek: str | None = None
    connectors_state_hmac_key: str | None = None
    idp_base_url: str | None = None
    # The in-process signing OIDC issuer's origin (the extended ``OAuthIdp``'s
    # ``base_url``) the oidc stack points its accounts-oidc / identity-oidc issuer
    # config at. ``None`` on every stack that runs no OIDC provider.
    oidc_issuer_base_url: str | None = None
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    # The in-process channel-provider stub origins the channel profile points the
    # plugins' outbound API base URLs at (``CHANNEL_<X>_API_BASE_URL``). ``None``
    # on every non-channel stack.
    telegram_api_base_url: str | None = None
    slack_api_base_url: str | None = None
    twilio_api_base_url: str | None = None
    whatsapp_api_base_url: str | None = None
    # The harness-run marketplace registry's public base URL the skeleton's
    # marketplace client points at (``MARKETPLACE_URL``), and the fixture package
    # index's origin the installer resolves wheels from (``PIP_INDEX_URL`` is
    # ``{package_index_url}/simple/``). ``None`` on every non-marketplace stack.
    marketplace_url: str | None = None
    package_index_url: str | None = None


@dataclass(frozen=True)
class StackConfig:
    """A frozen description of one stack: its shape, the rendered manifest, the
    feature env map, and which optional processes to run. Profiles in
    :mod:`tai42_e2e.manifests` are named presets of this."""

    name: str
    topology: Topology
    manifest: dict
    env: dict[str, str]
    # A verbatim manifest document seeded to disk BYTE-for-byte instead of
    # ``yaml.safe_dump(manifest)`` — the comment-preservation scenario seeds a
    # ruamel-normalized commented manifest this way (``safe_dump`` cannot carry
    # comments). ``None`` uses the dict-dump path.
    raw_manifest: str | None = None
    workers: int = 2
    run_backend: bool = True
    run_metrics: bool = True
    auth: bool = False
    # Serve the SUT as a user-owned embed host (uvicorn running the host FastAPI
    # app in ``tai42_e2e_fixtures.embed_main`` that mounts ``create_app()``) instead
    # of the ``tai serve`` fleet. One app process, in-process metrics mode; a
    # backend worker still joins the app-owned worker bus.
    embed: bool = False
    # Opt-in SUPERVISED shape: stamp ``TAI_SUPERVISED=harness`` into every child's env
    # (so the SUT resolves a recycle-supported shape, ``recycle_policy.detect_shape``) AND
    # run a respawn-on-exit supervisor that re-launches a serve/backend process the instant
    # it self-exits — the external supervisor a graceful recycle self-exit assumes (the
    # applier's own deferred self-exit and each orchestrated sibling recycle). Off by default
    # (bare): a recycle-class profile apply is then refused at the API, which is itself a test.
    supervised: bool = False
    # Per-process CWD overrides, keyed by process name (the shared-dir test launches
    # the three kinds from three different working directories).
    cwd_overrides: dict[str, str] = field(default_factory=dict)
    # Env keys that must carry this stack's own loopback app origins
    # (``http://host:port`` for every app port, comma-joined). The origins are
    # only known after boot allocates the ports, so a profile names the keys and
    # the stack fills them in at boot — e.g. the connectors profile pins
    # ``CONNECTORS_REDIRECT_URI_ALLOWLIST`` to the origins the OAuth connect flow
    # signs from ``request.base_url``.
    origin_allowlist_env_keys: list[str] = field(default_factory=list)
    # Env keys that must carry the SINGLE replica-B loopback origin
    # (``http://host:port_b``), only known after boot allocates the ports — the
    # channel profile pins ``INTERACTIONS_PUBLIC_BASE_URL`` (so an ask minted on
    # replica A is answered through the callback door on replica B) and the
    # telegram ``CHANNEL_TELEGRAM_PUBLIC_BASE_URL`` (the setWebhook URL) at B.
    # Only meaningful on a REPLICAS stack (two app ports).
    replica_b_origin_env_keys: list[str] = field(default_factory=list)
    # The REAL-inbound public-URL fill (empty on every mock leg). A real inbound
    # leg lists here the public-base-URL env keys (e.g. ``INTERACTIONS_PUBLIC_BASE_URL``,
    # ``CHANNEL_TELEGRAM_PUBLIC_BASE_URL``, ``TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL``)
    # that must carry ``E2E_PUBLIC_BASE_URL`` — the origin the vendor calls back on —
    # instead of the replica-B loopback origin (loopback is unreachable from the
    # vendor). Keys named here OVERRIDE the loopback fill, so a leg opts a key into
    # public mode by adding it here without touching ``replica_b_origin_env_keys``.
    # ``public_base_url`` must be set (from ``E2E_PUBLIC_BASE_URL``) whenever this is
    # non-empty; a real inbound leg refuses to start otherwise.
    public_base_url_env_keys: list[str] = field(default_factory=list)
    public_base_url: str | None = None
    # The REAL-inbound public-origin allowlist fill (empty on every mock leg). Mirrors
    # ``public_base_url_env_keys`` for the ``origin_allowlist_env_keys`` fill site: a real
    # connector leg lists here the allowlist keys (e.g. ``CONNECTORS_REDIRECT_URI_ALLOWLIST``)
    # that must carry the PUBLIC origin (``E2E_PUBLIC_BASE_URL``) — the origin the vendor
    # redirects the OAuth consent back to — instead of this stack's loopback app origins
    # (loopback is unreachable from the vendor). Keys named here OVERRIDE the loopback fill;
    # every key must also appear in ``origin_allowlist_env_keys``. ``public_base_url`` must be
    # set whenever this is non-empty; the leg refuses to start otherwise.
    public_allowlist_env_keys: list[str] = field(default_factory=list)
    # Env keys that must carry THIS stack's own single app origin (``http://host:port``),
    # only known after boot — the single-port (MULTIWORKER) analogue of
    # ``replica_b_origin_env_keys``. The studio profile pins ``INTERACTIONS_PUBLIC_BASE_URL``
    # here so an ask_user callback ticket it mints is reachable back on its own origin (the
    # web channel's answer door FORWARDS the answer to that callback URL — a loopback
    # origin resolves, an off-host placeholder does not). Filled from the first app port.
    app_origin_env_keys: list[str] = field(default_factory=list)


@dataclass
class _ProcSpec:
    """Everything needed to (re)spawn one process — kept so ``restart`` can
    rebuild an identical handle."""

    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    log_path: Path
