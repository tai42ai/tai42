"""Harness-wide settings: coordinates for the externally provided Redis and
Postgres plus the boot/teardown knobs. Read from the ``TAI_E2E_`` env space.

Also the REAL/MOCK switch: ``TAI_E2E_REAL`` selects, per external seam, whether
the leg runs against the live vendor or today's in-process mock, and
``REAL_SERVICES`` is the single source of truth for each seam's required
credentials. Empty ``TAI_E2E_REAL`` = every seam mock = today's suite unchanged;
the switch is inert until a service is named."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class LlmProvider:
    """One real-LLM provider the ``llm`` / ``embeddings`` seams can select.

    ``provider`` is the kit's ``LLM_PROVIDER_LLM`` / ``_EMBEDDING`` value (the
    ``tai42_kit.llm`` builders match on it); ``api_key_env`` is the credential
    template's env var holding that provider's key; ``default_llm_model`` /
    ``default_embedding_model`` are the fallback models when the operator leaves
    ``REAL_E2E_LLM_MODEL`` / ``REAL_E2E_EMBEDDING_MODEL`` unset.
    ``default_embedding_model`` is ``None`` for a chat-only provider — it cannot
    serve the embeddings seam."""

    provider: str
    api_key_env: str
    default_llm_model: str
    default_embedding_model: str | None


# The real-LLM providers the kit builders accept (``tai42_kit.llm.models.get_llm`` /
# ``embedding.get_embedding``), matched to the credential template's keys. A name
# the operator puts in ``REAL_E2E_LLM_PROVIDER`` / ``_EMBEDDING_PROVIDER`` must be a key
# here; an unknown name raises rather than silently falling back. Models carry cheap
# defaults the operator overrides per provider via ``REAL_E2E_*_MODEL``.
LLM_PROVIDERS: dict[str, LlmProvider] = {
    "openai": LlmProvider("openai", "OPENAI_API_KEY", "gpt-4o-mini", "text-embedding-3-small"),
    "anthropic": LlmProvider("anthropic", "ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001", None),
    "mistral": LlmProvider("mistral", "MISTRAL_API_KEY", "mistral-small-latest", "mistral-embed"),
    "google": LlmProvider("google", "GOOGLE_API_KEY", "gemini-2.0-flash", "models/text-embedding-004"),
    "xai": LlmProvider("xai", "XAI_API_KEY", "grok-3", None),
    "huggingface": LlmProvider(
        "huggingface",
        "HUGGINGFACEHUB_API_TOKEN",
        "HuggingFaceH4/zephyr-7b-beta",
        "sentence-transformers/all-MiniLM-L6-v2",
    ),
}


def _resolve_llm_provider(environ: Mapping[str, str], env_var: str, *, embeddings: bool) -> LlmProvider:
    name = (environ.get(env_var) or "openai").strip() or "openai"
    provider = LLM_PROVIDERS.get(name)
    if provider is None:
        valid = ", ".join(sorted(LLM_PROVIDERS))
        raise ValueError(f"{env_var}={name!r} is not a known LLM provider; valid providers: {valid}")
    if embeddings and provider.default_embedding_model is None:
        capable = ", ".join(p for p, s in sorted(LLM_PROVIDERS.items()) if s.default_embedding_model is not None)
        raise ValueError(f"{env_var}={name!r} does not serve embeddings; embedding providers: {capable}")
    return provider


def real_llm_provider(environ: Mapping[str, str]) -> LlmProvider:
    """The provider the real ``llm`` seam uses — ``REAL_E2E_LLM_PROVIDER`` (default
    ``openai``). An unknown name raises loudly at collection/build."""
    return _resolve_llm_provider(environ, "REAL_E2E_LLM_PROVIDER", embeddings=False)


def real_embedding_provider(environ: Mapping[str, str]) -> LlmProvider:
    """The provider the real ``embeddings`` seam uses — ``REAL_E2E_EMBEDDING_PROVIDER``
    (default ``openai``). Restricted to providers whose kit embedding builder exists; a
    chat-only provider (anthropic, xai) raises."""
    return _resolve_llm_provider(environ, "REAL_E2E_EMBEDDING_PROVIDER", embeddings=True)


@dataclass(frozen=True)
class RealService:
    """One external seam's real-leg contract.

    ``required_env`` is the exact set of env var names the operator must supply
    (non-empty) for the real leg — the loud-fail at collection names these when
    absent, and the real legs read the same list. ``inbound`` marks a seam whose
    real leg receives vendor callbacks (webhooks, OAuth redirects), so it needs a
    publicly reachable origin: ``E2E_PUBLIC_BASE_URL`` is required whenever any
    inbound seam is selected real. ``provider_seam`` marks the two
    provider-configurable seams (``llm`` / ``embeddings``): their required credential
    is the SELECTED provider's key (resolved from ``REAL_E2E_LLM_PROVIDER`` /
    ``_EMBEDDING_PROVIDER`` at collection), so the loud-fail names that provider's env
    var rather than a hardcoded one."""

    required_env: tuple[str, ...]
    inbound: bool
    provider_seam: Literal["llm", "embedding"] | None = None


# One entry per external seam (the ``TAI_E2E_REAL`` vocabulary). Names and
# credential lists derive from ``REAL_E2E_CREDENTIALS.env.example``; AUTO /
# harness-minted values are NOT required of the operator and are absent here.
# The WhatsApp verify token IS required: it is an operator-chosen shared secret
# registered in Meta's dashboard subscription and read verbatim by the plugin, so
# the real leg loud-fails without it (never harness-minted).
REAL_SERVICES: dict[str, RealService] = {
    "slack": RealService(
        required_env=("CHANNEL_SLACK_BOT_TOKEN", "CHANNEL_SLACK_SIGNING_SECRET", "CHANNEL_SLACK_TEST_CHANNEL_ID"),
        inbound=True,
    ),
    "telegram": RealService(
        required_env=("CHANNEL_TELEGRAM_BOT_TOKEN", "CHANNEL_TELEGRAM_TEST_CHAT_ID"),
        inbound=True,
    ),
    "twilio": RealService(
        required_env=(
            "CHANNEL_TWILIO_ACCOUNT_SID",
            "CHANNEL_TWILIO_AUTH_TOKEN",
            "CHANNEL_TWILIO_FROM",
            "CHANNEL_TWILIO_TEST_TO",
        ),
        inbound=True,
    ),
    "whatsapp": RealService(
        required_env=(
            "CHANNEL_WHATSAPP_ACCESS_TOKEN",
            "CHANNEL_WHATSAPP_APP_SECRET",
            "CHANNEL_WHATSAPP_VERIFY_TOKEN",
            "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID",
            "CHANNEL_WHATSAPP_TEST_TO",
        ),
        inbound=True,
    ),
    # llm and embeddings toggle independently (each replaces only its own env group)
    # and are provider-configurable: the required credential is the selected provider's
    # key (``LLM_PROVIDERS``), so ``required_env`` is empty here and the provider seam
    # resolves it at collection. Default provider is openai (covers both chat and
    # embeddings).
    "llm": RealService(required_env=(), inbound=False, provider_seam="llm"),
    "embeddings": RealService(required_env=(), inbound=False, provider_seam="embedding"),
    "connector-google": RealService(
        required_env=(
            "CONNECTORS_GOOGLE_CLIENT_ID",
            "CONNECTORS_GOOGLE_CLIENT_SECRET",
            "CONNECTORS_GOOGLE_TEST_ACCOUNT_EMAIL",
        ),
        inbound=True,
    ),
    "connector-atlassian": RealService(
        required_env=(
            "CONNECTORS_ATLASSIAN_CLIENT_ID",
            "CONNECTORS_ATLASSIAN_CLIENT_SECRET",
            "CONNECTORS_ATLASSIAN_SITE_URL",
            "CONNECTORS_ATLASSIAN_TEST_ACCOUNT_EMAIL",
        ),
        inbound=True,
    ),
    "storage-s3": RealService(
        required_env=(
            "STORAGE_S3_BUCKET",
            "STORAGE_S3_REGION",
            "STORAGE_S3_ACCESS_KEY",
            "STORAGE_S3_SECRET_KEY",
            "STORAGE_S3_ENDPOINT",
        ),
        inbound=False,
    ),
    "storage-github": RealService(
        required_env=("STORAGE_GITHUB_USERNAME", "STORAGE_GITHUB_REPO", "STORAGE_GITHUB_TOKEN"),
        inbound=False,
    ),
    "oidc": RealService(
        required_env=(
            "AUTH0_ISSUER",
            "AUTH0_CLIENT_ID",
            "AUTH0_CLIENT_SECRET",
            "AUTH0_AUDIENCE",
            "AUTH0_TEST_USER_EMAIL",
            "AUTH0_TEST_USER_PASSWORD",
        ),
        inbound=True,
    ),
    "github-login": RealService(
        required_env=("GITHUB_LOGIN_CLIENT_ID", "GITHUB_LOGIN_CLIENT_SECRET"),
        inbound=True,
    ),
    # The real leg runs against the operator's managed cluster: KUBECONFIG addresses
    # it and TAI_K8S_NAMESPACE names the namespace the operator seeded the
    # ConfigMap/Secret in (the hermetic fake-apiserver leg pins its own ``e2e``
    # namespace and needs neither).
    "k8s": RealService(required_env=("KUBECONFIG", "TAI_K8S_NAMESPACE"), inbound=False),
    "langfuse": RealService(
        required_env=("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"),
        inbound=False,
    ),
    "stripe": RealService(
        required_env=("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"),
        inbound=True,
    ),
    "marketplace-github": RealService(required_env=("MP_GITHUB_TOKEN",), inbound=False),
    # Real pypi.org is public — no operator credential; the real leg only repoints
    # the index env keys, so there is nothing to loud-fail on.
    "marketplace-pypi": RealService(required_env=(), inbound=False),
}


# The storage seams are dual-knob: the storage AXIS (``TAI_E2E_STORAGE``) picks the
# variant that actually boots, while the ``TAI_E2E_REAL`` name only feeds the credential
# loud-fail. This maps each real-storage ``TAI_E2E_REAL`` seam to the storage-axis variant
# it requires, so a mismatch (real seam named, axis still mock) fails loudly instead of
# silently running the hermetic mock.
STORAGE_REAL_AXIS: dict[str, str] = {"storage-s3": "s3-real", "storage-github": "github-real"}


class HarnessSettings(BaseSettings):
    """Infra coordinates the harness itself connects to. These describe the
    Redis and Postgres that ``docker compose up -d`` (locally) or the CI
    ``services:`` block provide; the harness owns only logical isolation on top
    of them and never talks to Docker."""

    # ``populate_by_name`` so a field carrying an explicit env alias (public_base_url
    # <- E2E_PUBLIC_BASE_URL) is still constructible by its field name in-process.
    model_config = SettingsConfigDict(env_prefix="TAI_E2E_", extra="ignore", populate_by_name=True)

    # Which plugin variant this pytest process runs under. One set per process,
    # selected here; ``tai42_e2e.variants.resolve_variants`` maps each to its
    # adapter and fails loudly on an unknown name.
    backend: str = "arq"  # arq | celery | rq
    identity: str = "redis"  # redis | fixture
    storage: str = "local"  # local | fixture

    # The RabbitMQ the celery backend isolates each stack on (a per-stack vhost).
    # Validated/used only when ``backend == "celery"``; the AMQP URL feeds the
    # stack broker URL, the management URL provisions and reaps the vhost.
    rabbitmq_url: str = "amqp://guest:guest@127.0.0.1:5672"
    rabbitmq_management_url: str = "http://guest:guest@127.0.0.1:15672"

    # The shared Redis. Every stack is pinned to a logical DB index under this
    # server; the harness never appends a DB index here itself.
    redis_url: str = "redis://127.0.0.1:6379"

    # The module-capable Redis (RediSearch + RedisJSON) the langgraph redis
    # checkpoint/store provider requires (the plain shared ``redis_url`` lacks the
    # modules). Unset by default: the agents-redis checkpoint suite skips with a compose
    # hint when absent. Each stack that needs it is pinned to its own logical DB here.
    checkpoint_redis_url: str | None = None

    # The shared Postgres admin connection. The harness connects here to create
    # the template DB and per-stack CREATE DATABASE / DROP DATABASE.
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    # The maintenance DB to connect to for CREATE/DROP DATABASE statements.
    pg_admin_db: str = "postgres"

    # Deadline for a stack to reach readiness (all app ports healthy, metrics
    # up, backend present in the census).
    boot_timeout: float = 30.0

    # Keep per-stack config + log dirs after teardown for debugging.
    keep_stacks: bool = Field(default=False)

    # Opt in to the monitoring suite (requires the compose ``monitoring``
    # profile to be up). When false, tests/monitoring is skipped at collection.
    monitoring: bool = Field(default=False)

    # Opt in to the marketplace suite (installs the tai42-marketplace registry
    # out-of-band from its pinned git source at boot and runs it as harness-managed
    # processes against the shared Redis/Postgres). When false, tests/marketplace
    # is skipped at collection.
    marketplace: bool = Field(default=False)

    # Opt in to the fleet-consistency suite (boots the multi-worker / REPLICAS stacks
    # under the app worker bus). When false, tests/fleet is skipped at collection.
    fleet: bool = Field(default=False)

    # The REAL/MOCK switch: a comma-separated list of external seams
    # (``TAI_E2E_REAL=slack,llm,stripe``) whose leg runs against the live vendor
    # instead of its in-process mock. Every name must be a ``REAL_SERVICES`` key;
    # an unknown name raises rather than silently mocking. Empty (the default) =
    # every seam mock = today's suite byte-for-byte.
    real: str = ""

    # The public HTTPS origin the real inbound legs register with vendors and
    # receive callbacks on (``E2E_PUBLIC_BASE_URL`` — no ``TAI_E2E_`` prefix, it is
    # the operator/domain-facing knob shared with the credential template). Unset
    # by default; an inbound real selection that lacks it fails loudly.
    public_base_url: str | None = Field(default=None, validation_alias=AliasChoices("E2E_PUBLIC_BASE_URL"))

    @property
    def real_services(self) -> frozenset[str]:
        """The parsed ``TAI_E2E_REAL`` set. An unknown service name raises loudly
        (a typo must never silently fall back to mock)."""
        names = [part.strip() for part in self.real.split(",") if part.strip()]
        unknown = sorted(set(names) - REAL_SERVICES.keys())
        if unknown:
            valid = ", ".join(sorted(REAL_SERVICES))
            raise ValueError(f"TAI_E2E_REAL names unknown service(s): {', '.join(unknown)}; valid services: {valid}")
        return frozenset(names)

    def is_real(self, service: str) -> bool:
        """Whether ``service`` runs its real leg this process. The single predicate
        the per-service real legs branch on. An unknown name raises."""
        if service not in REAL_SERVICES:
            valid = ", ".join(sorted(REAL_SERVICES))
            raise ValueError(f"unknown service {service!r}; valid services: {valid}")
        return service in self.real_services

    @property
    def real_inbound_services(self) -> frozenset[str]:
        """The selected real seams that receive vendor callbacks — the ones that
        make ``E2E_PUBLIC_BASE_URL`` mandatory."""
        return frozenset(name for name in self.real_services if REAL_SERVICES[name].inbound)

    def missing_real_creds(self, environ: Mapping[str, str]) -> dict[str, list[str]]:
        """For every selected real seam, the required env vars that are absent or
        empty in ``environ`` — keyed by service, empty when fully configured. The
        loud-fail names exactly these. For the provider-configurable ``llm`` /
        ``embeddings`` seams the required key is the SELECTED provider's (an unknown
        provider name raises here, loudly, at collection)."""
        missing: dict[str, list[str]] = {}
        for name in sorted(self.real_services):
            service = REAL_SERVICES[name]
            required = list(service.required_env)
            if service.provider_seam == "llm":
                required.append(real_llm_provider(environ).api_key_env)
            elif service.provider_seam == "embedding":
                required.append(real_embedding_provider(environ).api_key_env)
            absent = [var for var in required if not environ.get(var)]
            if absent:
                missing[name] = absent
        return missing

    def storage_axis_mismatches(self) -> list[str]:
        """The coherence problems between the two storage knobs. ``TAI_E2E_STORAGE``
        (the storage axis) picks the variant that actually boots; ``storage-s3`` /
        ``storage-github`` in ``TAI_E2E_REAL`` only feed the credential loud-fail. So a
        real storage seam named on ``TAI_E2E_REAL`` WITHOUT the axis set to its matching
        real variant would pass the cred gate yet run the hermetic mock — a silent
        no-op. This reports that mismatch so collection can fail loudly; empty when the
        two knobs agree (or no real storage seam is selected)."""
        problems: list[str] = []
        for seam, variant in STORAGE_REAL_AXIS.items():
            if seam in self.real_services and self.storage != variant:
                problems.append(
                    f"{seam} is selected real on TAI_E2E_REAL but TAI_E2E_STORAGE={self.storage!r} "
                    f"is not its real variant (set TAI_E2E_STORAGE={variant} to run the real storage leg, "
                    f"or drop {seam} from TAI_E2E_REAL to run the hermetic mock)"
                )
        return problems

    @property
    def redis_host_port(self) -> tuple[str, int]:
        """The (host, port) of the shared Redis, parsed from ``redis_url``."""
        return _host_port(self.redis_url, "TAI_E2E_REDIS_URL")

    @property
    def checkpoint_redis_host_port(self) -> tuple[str, int]:
        """The (host, port) of the module-capable checkpoint Redis, parsed from
        ``checkpoint_redis_url``. Only read when the URL is set."""
        if self.checkpoint_redis_url is None:
            raise ValueError("checkpoint_redis_url is unset; the checkpoint Redis is not configured")
        return _host_port(self.checkpoint_redis_url, "TAI_E2E_CHECKPOINT_REDIS_URL")


def _host_port(url: str, env_var: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"{env_var} must include host and port: {url!r}")
    return parsed.hostname, parsed.port
