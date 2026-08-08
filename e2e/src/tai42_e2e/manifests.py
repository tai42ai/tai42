"""Typed builders for the manifest + feature-env of each stack profile.

A profile is a named :class:`~tai42_e2e.stack.StackConfig`: a manifest dict (the
YAML the SUT loads) plus the feature-env map that points every ``*_REDIS_URL`` /
``*_PG_*`` / plugin setting at this stack's isolated resources. The pytest
fixtures in ``tests/conftest.py`` allocate the resources and call these."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from tai42_e2e.harness import STUDIO_PATH_PATTERNS
from tai42_e2e.settings import HarnessSettings, real_embedding_provider, real_llm_provider
from tai42_e2e.stack import StackConfig, StackResources, Topology, venv_console_script

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


def _switch() -> HarnessSettings:
    """The REAL/MOCK switch for this pytest process, read fresh from the ambient
    ``TAI_E2E_`` env (the same construction the published plugin uses). Empty
    ``TAI_E2E_REAL`` = every seam mock, so every ``is_real`` branch below is inert
    and the rendered env is byte-for-byte today's — the switch is a no-op until a
    seam is named. The per-service real legs branch on ``settings.is_real(seam)``;
    the collection-time gate has already loud-failed any selected seam whose
    credentials (or public base URL) are absent, so a real branch here reads its
    operator env unconditionally."""
    return HarnessSettings()


# The manifest title the probe tools load under; the prometheus extension stamps
# it as the ``title`` label, so metrics assertions reference this constant.
PROBE_TOOLS_TITLE = "e2e-probes"

# Toolbox extension branch modules whose import registers the WRAPPER/TRANSFORMER
# branches the suite attaches (prometheus_metrics/batch/proxy). The BACKEND
# branches (sync_task/...) register with the backend_module import, not here.
_EXTENSION_MODULES = [
    "tai42_toolbox.extensions.prometheus",
    "tai42_toolbox.extensions.batch",
    "tai42_toolbox.extensions.proxy",
]

# Router modules the suite drives — every HTTP route is opt-in at its module import.
# Curated stacks pin ``default_routers="none"`` alongside this list so their served
# surface stays exactly what they list, never ballooning to the skeleton's ``"all"``
# default set.
_CORE_ROUTERS = [
    "tai42_skeleton.routers.health",
    "tai42_skeleton.routers.metrics",
    "tai42_skeleton.routers.tools",
    "tai42_skeleton.routers.config",
    "tai42_skeleton.routers.manifest",
    "tai42_skeleton.routers.hooks",
    "tai42_skeleton.routers.tool_runs",
    "tai42_skeleton.routers.schedules",
    "tai42_skeleton.routers.interactions",
    "tai42_skeleton.routers.sub_mcp",
    "tai42_skeleton.routers.tool_extensions",
    "tai42_skeleton.routers.presets",
    "tai42_skeleton.routers.tool_meta",
    "tai42_skeleton.routers.extensions",
    "tai42_skeleton.routers.templates",
    "tai42_skeleton.routers.storage",
    "tai42_skeleton.routers.backend",
]


def _probe_tools_entry(
    *, with_backend_branches: bool, with_schedule_branch: bool = False, with_monitor_branch: bool = False
) -> dict:
    """The SUT-side probe tools entry. ``with_backend_branches`` attaches the
    ``sync_task`` combos (needs a backend stack). ``with_schedule_branch`` attaches
    ``schedule_task`` to ``e2e_record`` — enabled only on ``build_schedule_stack``,
    never on the reload-heavy ``replicas`` profile (on celery a ``schedule_task``
    tool riding reload churn can leave the prefork pool unable to dispatch)."""
    extensions: dict[str, list[list[str]]] = {
        "e2e_echo": [["prometheus_metrics"]],
        "e2e_fail": [["prometheus_metrics"]],
        "e2e_http_probe": [["proxy"]],
    }
    if with_backend_branches:
        # Chain sync_task onto the prometheus-wrapped echo so the counter-wrapped tool
        # runs inside the backend worker, and attach it to the worker-info probe.
        extensions["e2e_echo"] = [["prometheus_metrics"], ["prometheus_metrics", "sync_task"]]
        extensions["e2e_worker_info"] = [["sync_task"]]
        # Branch the record-then-block probe so the worker-crash spec can start a run
        # observably in-flight in the worker, SIGKILL it, and read a bounded terminal.
        extensions["e2e_slow_task"] = [["sync_task"]]
        # Branch the drain probe (started → sleep → done) so the recycle-drain spec can read
        # whether an in-flight backend job DRAINED to completion across a recycle.
        extensions["e2e_drain_probe"] = [["sync_task"]]
        # Universal backend-execution door: run_tool_sync_task runs any registered tool
        # by name inside the backend worker. Its base is the FIXTURE ``run_tool`` dispatch
        # tool (the skeleton ``run_tool`` op is a tier-1 meta-executor blocked from the MCP
        # surface, so it has no projected base to wrap).
        extensions["run_tool"] = [["sync_task"]]
    if with_schedule_branch:
        # Branch e2e_record into e2e_record_schedule_task: a recurring schedule that runs
        # e2e_record each firing, so the scheduling spec reads periodicity off the channel.
        extensions["e2e_record"] = [["schedule_task"]]
        # Branch run_tool into run_tool_schedule_task: the scheduling analog of
        # run_tool_sync_task — schedules any tool by name (the sweep-schedulable leg).
        extensions["run_tool"] = [*extensions.get("run_tool", []), ["schedule_task"]]
    if with_monitor_branch:
        # Trace a standalone e2e_echo call as one TOOL span, giving the observability read
        # surface a run to serve back (build_monitoring_stack's langfuse records it).
        extensions["e2e_echo"] = [*extensions["e2e_echo"], ["monitor"]]
    return {"title": PROBE_TOOLS_TITLE, "module": "tai42_e2e_fixtures.tools", "extensions": extensions}


# The ask_user HITL builtin tool module. Other management ops (reload_config,
# reload_mcp, register_hook, templates, notify_user) project onto the MCP tool surface
# from the operations registry via ``api_tools`` (see ``_PROJECTED_API_TOOLS``).
_INTERACTIONS_ENTRY = {"title": "builtin-interactions", "module": "tai42_skeleton.tools.builtin.interactions"}

# Projects the management operations onto the MCP tool surface with the default curation:
# destructive ops exposed, the tier-1 ``run_tool`` blocked, tier-2 ``/api/auth/*`` ops
# default-excluded. Which ops project is scoped by the profile's mounted routers.
_PROJECTED_API_TOOLS = {"enabled": True}


def _builtin_entries() -> list[dict]:
    """The builtin ``tools[]`` entries a profile carries: ``builtin-interactions``
    (ask_user). Management ops project via ``api_tools`` instead."""
    return [_INTERACTIONS_ENTRY]


def _toolbox_tools_entry() -> dict:
    return {
        "title": "toolbox",
        "module": "tai42_toolbox.tools.generate_uuid",
        "include": ["generate_uuid"],
    }


# Each toolbox tool lives in its own module (one tool per module), so a profile
# names the module and ``include``s the one tool it registers. ``request`` needs the
# ``http`` extra (already in the e2e env) and ``generate_embeddings`` the ``embeddings``
# extra (a PLAN_1 dep addition) — both fail LOUDLY at import when their extra is absent,
# so a stack carrying them refuses to boot rather than silently dropping the tool.
_TOOLBOX_EXTRA_TOOL_ENTRIES: list[dict] = [
    {"title": "toolbox-request", "module": "tai42_toolbox.tools.request", "include": ["request"]},
    {
        "title": "toolbox-embeddings",
        "module": "tai42_toolbox.tools.generate_embeddings",
        "include": ["generate_embeddings"],
    },
    {"title": "toolbox-pad-embeddings", "module": "tai42_toolbox.tools.pad_embeddings", "include": ["pad_embeddings"]},
    {
        "title": "toolbox-current-time",
        "module": "tai42_toolbox.tools.current_time_info",
        "include": ["current_time_info"],
    },
]


def _redis_feature_env(res: StackResources) -> dict[str, str]:
    """Point every per-feature Redis URL at the stack's logical DB, and the
    probe-record client at DB 0."""
    return {
        "ACCESS_CONTROL_REDIS_URL": res.redis_url,
        "INTERACTIONS_REDIS_URL": res.redis_url,
        "TAI_TOOL_RUNS_REDIS_URL": res.redis_url,
        "TAI_RATE_LIMIT_REDIS_URL": res.redis_url,
        "HOOKS_REDIS_URL": res.redis_url,
        "SUB_MCP_REDIS_URL": res.redis_url,
        "CONNECTOR_STORE_REDIS_URL": res.redis_url,
        "E2E_PROBE_REDIS_URL": res.probe_redis_url,
    }


# Every agent in the reference package, one manifest entry each (own module, so
# ``include`` names the one agent that module registers). All run on the scripted LLM
# stub + local stack resources.
_AGENT_ENTRIES: list[dict] = [
    {"title": "tai-agents-tools", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]},
    {"title": "tai-agents-deep", "module": "tai42_agents.deep_agent", "include": ["deep_agent"]},
    {"title": "tai-agents-refine", "module": "tai42_agents.refine_agent", "include": ["refine_agent"]},
    {"title": "tai-agents-voting", "module": "tai42_agents.voting_agent", "include": ["voting_agent"]},
    {"title": "tai-agents-mcp-tools", "module": "tai42_agents.mcp_tools_agent", "include": ["mcp_tools_agent"]},
    {
        "title": "tai-agents-retrieval",
        "module": "tai42_agents.retrieval_tools_agent",
        "include": ["retrieval_tools_agent"],
    },
    {"title": "tai-agents-vqa", "module": "tai42_agents.vqa_agent", "include": ["vqa_agent"]},
]


def _llm_env(res: StackResources) -> dict[str, str]:
    """The agents' model + embedding access, per the ``llm`` / ``embeddings`` seams.

    MOCK (default): both groups point at the scripted stub — it serves
    ``/v1/chat/completions`` and ``/v1/embeddings`` off one origin, so they share
    ``res.llm_base_url`` (empty when the stub URL is unset — the studio profile
    allocates the stub port separately).

    REAL: ``llm`` and ``embeddings`` toggle INDEPENDENTLY, each replacing only its
    own group with the live provider. Both are PROVIDER-CONFIGURABLE (HARNESS-MAP):
    ``REAL_E2E_LLM_PROVIDER`` / ``REAL_E2E_EMBEDDING_PROVIDER`` (default ``openai``)
    picks the provider from ``LLM_PROVIDERS``; the harness sets ``LLM_PROVIDER_LLM`` /
    ``_EMBEDDING`` to the provider id, maps that provider's template key
    (``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` / …) to ``LLM_API_KEY`` /
    ``EMBEDDING_API_KEY``, and sets the model from ``REAL_E2E_*_MODEL`` (else the
    provider default). LangChain's native-env fallbacks are never relied on. A group
    left mock still points at the stub, so a real-``llm`` / mock-``embeddings`` mix is
    exact."""
    switch = _switch()
    env: dict[str, str] = {}
    if switch.is_real("llm"):
        provider = real_llm_provider(os.environ)
        env["LLM_PROVIDER_LLM"] = provider.provider
        env["LLM_API_KEY"] = os.environ[provider.api_key_env]
        env["LLM_MODEL"] = os.environ.get("REAL_E2E_LLM_MODEL", provider.default_llm_model)
    elif res.llm_base_url is not None:
        env["LLM_BASE_URL"] = res.llm_base_url
        env["LLM_API_KEY"] = "e2e-test"
        env["LLM_MODEL"] = "e2e-scripted"
    if switch.is_real("embeddings"):
        provider = real_embedding_provider(os.environ)
        env["LLM_PROVIDER_EMBEDDING"] = provider.provider
        env["EMBEDDING_API_KEY"] = os.environ[provider.api_key_env]
        # A provider reaching the embeddings seam always has a non-None default model.
        default_embedding_model = provider.default_embedding_model or ""
        env["EMBEDDING_MODEL"] = os.environ.get("REAL_E2E_EMBEDDING_MODEL", default_embedding_model)
    elif res.llm_base_url is not None:
        env["EMBEDDING_BASE_URL"] = res.llm_base_url
        env["EMBEDDING_API_KEY"] = "e2e-test"
        env["EMBEDDING_MODEL"] = "e2e-embed"
    return env


def _memory_agent_state_env() -> dict[str, str]:
    """Pin the agent's langgraph checkpoint + long-term store to the in-process
    ``memory`` provider. The default ``redis`` provider's saver builds RediSearch
    indexes on setup, which the module-free plain ``redis:7-alpine`` rejects; a single
    run's checkpoint/store lives only for the run, so in-process is faithful here."""
    return {
        "LLM_PROVIDER_CHECKPOINT": "memory",
        "LLM_PROVIDER_STORE": "memory",
    }


def _redis_agent_state_env(res: StackResources) -> dict[str, str]:
    """Pin the agent's langgraph checkpoint + long-term store to the production default
    ``redis`` provider on the module-capable checkpoint Redis, this stack's logical DB.
    The saver/store build RediSearch indexes + RedisJSON on setup, which the plain shared
    ``redis:7-alpine`` rejects. Both connection strings target the same DB (their key
    namespaces do not collide)."""
    if res.checkpoint_redis_url is None:
        raise RuntimeError(
            "build_agents_redis_stack requires resources.checkpoint_redis_url; allocate_resources must run "
            "with allocate_checkpoint_db=True (and TAI_E2E_CHECKPOINT_REDIS_URL must be set)"
        )
    return {
        "LLM_PROVIDER_CHECKPOINT": "redis",
        "LLM_PROVIDER_CHECKPOINT_CONN_STRING": res.checkpoint_redis_url,
        "LLM_PROVIDER_STORE": "redis",
        "LLM_PROVIDER_STORE_CONN_STRING": res.checkpoint_redis_url,
    }


def _pg_env(prefix: str, res: StackResources) -> dict[str, str]:
    """The five ``<prefix>PG_*`` connection keys — the ``TAI_DATABASE_DEFAULT_`` named
    database the platform binds every store to, or the external postgres-mcp's bare
    ``PG_`` env."""
    return {
        f"{prefix}PG_HOST": res.pg_host,
        f"{prefix}PG_PORT": str(res.pg_port),
        f"{prefix}PG_DB": res.pg_db,
        f"{prefix}PG_USER": res.pg_user,
        f"{prefix}PG_PASSWORD": res.pg_password,
    }


def _base_env(res: StackResources, variants: Variants) -> dict[str, str]:
    env = _redis_feature_env(res)
    # The backend + storage plugins' env groups, pointed at this stack's isolated
    # resources by the selected variants.
    env.update(variants.backend.feature_env(res))
    env.update(variants.storage.feature_env(res))
    # The proxy probe tool routes a caller-supplied URL through the harness proxy;
    # opt into caller URLs so the proxy extension routes it instead of refusing.
    env["PROXY_ALLOW_CALLER_URLS"] = "true"
    # Harness targets all bind 127.0.0.1, which the SSRF/URL guard refuses by default.
    # Opt the loopback ranges in, keeping the guard ON for everything else.
    env["TAI_URL_GUARD_ALLOW_CIDRS"] = json.dumps(["127.0.0.0/8", "::1/128"])
    # Every skeleton store (versioning, tool_meta, connectors, marketplace, access
    # control) binds to the ``default`` named database, which points at this stack's
    # isolated PG clone. Declaring the default database configures them all — a store
    # is live iff its bound database is configured.
    env.update(_pg_env("TAI_DATABASE_DEFAULT_", res))
    # Off by default here; the auth/accounts profiles turn it back on after calling this.
    env["ACCESS_CONTROL_ENABLE"] = "false"
    return env


# ---- profiles -----------------------------------------------------------


def build_minimal_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The smallest bootable stack — one worker, no backend — for the harness
    self-tests (boot/teardown/leak-safety)."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [
            "tai42_skeleton.routers.health",
            "tai42_skeleton.routers.metrics",
            "tai42_skeleton.routers.tools",
            "tai42_skeleton.routers.config",
        ],
        # The probe entry attaches an ``e2e_http_probe: [["proxy"]]`` branch, so the
        # proxy extension module must load or startup extension-validation aborts.
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "tools": [_probe_tools_entry(with_backend_branches=False)],
        # No management surface: projection off so the mounted config/tools ops do not
        # project as MCP tools. Keeps this the smallest bootable stack.
        "api_tools": {"enabled": False},
    }
    return StackConfig(
        name="minimal",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_bare_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The full ``_CORE_ROUTERS`` surface MOUNTED but with NO storage provider and NO
    backend registered — the honest absent-provider profile the storage/backend doors'
    ``present: false`` / 501 assertions drive. One worker, no backend, auth off."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    return StackConfig(
        name="bare",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_core_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(2) + backend + metrics — the metrics round-trip / import-order
    home. Auth off."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            _toolbox_tools_entry(),
            # The four previously-uncovered toolbox tools (request / generate_embeddings /
            # pad_embeddings / current_time_info) load on the core profile — its tests drive
            # ``request`` against the harness target server and the embeddings tools against
            # the LLM stub's ``/v1/embeddings`` via the tool's per-call ``base_url``.
            *_TOOLBOX_EXTRA_TOOL_ENTRIES,
            {"title": "builtin-file-loader", "module": "tai42_skeleton.tools.builtin.file_loader"},
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    return StackConfig(
        name="core",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=2,
        run_backend=True,
        run_metrics=True,
        auth=False,
    )


def build_embed_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The embed deployment shape: a user-owned ``uvicorn`` host (the FastAPI app
    in ``tai42_e2e_fixtures.embed_main``) mounting ``create_app()``, plus one backend
    worker on the control-plane bus. Auth off. No ``tai metrics`` sidecar —
    ``run_metrics=False`` — because the embed app serves the in-process
    ``/metrics`` registry itself, which is the surface under test."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            _toolbox_tools_entry(),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    return StackConfig(
        name="embed",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=True,
        run_metrics=False,
        auth=False,
        embed=True,
    )


def build_replicas_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + backend + metrics — every cross-worker / Redis-contention test
    and the reload suite. Loads the github webhook verifier and a per-stack webhook
    secret.

    Carries no ``schedule_task`` probe branch and no scheduler process — held apart from
    the scheduling stack because on celery a ``schedule_task`` tool riding this profile's
    reload churn can leave the prefork pool unable to dispatch. Scheduling coverage lives
    on ``build_schedule_stack``."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": ["tai42_webhook_verifier_github", "tai42_skeleton.webhooks.builtin.shared_secret"],
        # The stripe verifier rides the canonical binding field for the webhook-verifier
        # kind; the github verifier keeps its pre-canonical ``lifecycle_modules`` slot
        # above (both loaders read either), so the two verifiers coexist on this stack.
        "webhook_verifier_modules": ["tai42_webhook_verifier_stripe"],
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            _toolbox_tools_entry(),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    if res.gh_webhook_secret is not None:
        env["E2E_GH_WEBHOOK_SECRET"] = res.gh_webhook_secret
    if res.stripe_webhook_secret is not None:
        env["E2E_STRIPE_WEBHOOK_SECRET"] = res.stripe_webhook_secret
    # An external-format ``ask_user`` mints a callback ticket only when a public base URL
    # is set (it builds the callback URL from it); the host is never dialed, but the
    # setting requires an https value.
    env["INTERACTIONS_PUBLIC_BASE_URL"] = "https://e2e.local"
    return StackConfig(
        name="replicas",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=False,
    )


def build_recycle_stack(res: StackResources, variants: Variants) -> StackConfig:
    """SUPERVISED MULTIWORKER(1) + backend — the settings-profile RECYCLE leg.

    One serve worker (the applier) plus one backend runtime on the bus, carrying the
    skeleton component store (profiles) and the sync-task probe branch (an observably
    in-flight backend job). ``supervised=True`` stamps ``TAI_SUPERVISED=harness`` into every
    child (a recycle-supported shape) and runs the harness respawn-on-exit supervisor, so a
    recycle self-exit (the applier's own deferred exit AND each orchestrated sibling) is
    re-launched and rejoins the census under a fresh origin.

    ``BACKEND_MANIFEST_KEY`` / ``BACKEND_TOOL_NAME_ARG`` are the recycle-class fields the flip
    test diffs (``reload_class="recycle"``, not in any refused tier on the ``harness`` shape).
    The recycle step budget is widened so a real worker RESPAWN boot fits inside the per-step
    census wait ``orchestrate_recycle`` allows the replacement."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    # A recycled worker's replacement must boot + rejoin the census within the recycle
    # orchestrator's per-step budget (``shutdown_drain_seconds``); a fresh process boot is far
    # slower than the 10s default, so widen it for this leg.
    env["TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS"] = "90"
    return StackConfig(
        name="recycle",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=True,
        run_metrics=False,
        auth=False,
        supervised=True,
    )


# The channel profile's per-medium recipient policy. In every medium the default
# recipient is deliberately absent from the allowlist: the allowlist gates only
# caller-supplied recipients, never the operator default, so a default-recipient send
# passing is itself the trusted-default proof.
TELEGRAM_DEFAULT_RECIPIENT = "910001"
TELEGRAM_ALLOWED_RECIPIENTS = ("910002", "910003")
TELEGRAM_UNLISTED_RECIPIENT = "990009"
SLACK_DEFAULT_RECIPIENT = "C0DEFAULT0"
SLACK_ALLOWED_RECIPIENTS = ("C0ALLOWED1", "C0ALLOWED2")
SLACK_UNLISTED_RECIPIENT = "C0UNLISTED"
TWILIO_FROM = "+15559999999"
TWILIO_DEFAULT_RECIPIENT = "+15550000100"
TWILIO_ALLOWED_RECIPIENTS = ("+15550000200", "+15550000300")
TWILIO_UNLISTED_RECIPIENT = "+15559990009"

# The web channel carries NO recipient policy of its own: it has no operator default and
# no allowlist, because a web "recipient" is not an operator-chosen address but the
# visitor's own session — a delivery names ``"<web route identity>:<visitor session id>"``
# and the cookie holding that id is the only credential that can read or answer it.
WEB_IDENTITY = "e2e-web-site"

# Concurrent SSE streams one visitor may hold on a web stack, pinned into the profile env
# so the stream-cap leg drives a number the harness owns rather than the plugin's default.
WEB_MAX_STREAMS_PER_VISITOR = 4

# How often ONE web question's record may be put back after a refused or failed answer
# forward. No leg drives a refused forward, so this only has to stay clear of an accidental
# re-answer; pinned rather than inherited so the number the suite runs on is the harness's.
WEB_MAX_ANSWER_RESTORES = 50

# Bytes read from a web POST body before the door refuses with 413, pinned for the same
# reason: the body-cap leg reads this back instead of restating the plugin's default.
WEB_MAX_BODY_BYTES = 65536


# The channel seams whose real leg swaps the recording stub for the live vendor:
# each drops its ``CHANNEL_<X>_API_BASE_URL`` (the plugin default is the real vendor
# host) and reads its bot credential + test recipient from the operator template.
_CHANNEL_SEAMS = ("telegram", "slack", "twilio")


def _telegram_channel_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: TEST_CHAT_ID -> default + sole allowlisted recipient. No
        # API_BASE_URL (plugin default = real api.telegram.org); the webhook secret
        # is harness-minted (Telegram echoes whatever we register with setWebhook).
        chat = os.environ["CHANNEL_TELEGRAM_TEST_CHAT_ID"]
        return {
            "CHANNEL_TELEGRAM_BOT_TOKEN": os.environ["CHANNEL_TELEGRAM_BOT_TOKEN"],
            "CHANNEL_TELEGRAM_WEBHOOK_SECRET": secrets.token_hex(16),
            "CHANNEL_TELEGRAM_REDIS_URL": res.redis_url,
            "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT": chat,
            "CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS": chat,
        }
    return {
        "CHANNEL_TELEGRAM_BOT_TOKEN": "e2e-telegram-bot-token",
        "CHANNEL_TELEGRAM_WEBHOOK_SECRET": secrets.token_hex(16),
        "CHANNEL_TELEGRAM_API_BASE_URL": _require_stub(res.telegram_api_base_url, "telegram"),
        "CHANNEL_TELEGRAM_REDIS_URL": res.redis_url,
        "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT": TELEGRAM_DEFAULT_RECIPIENT,
        "CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS": ",".join(TELEGRAM_ALLOWED_RECIPIENTS),
    }


def _slack_channel_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: TEST_CHANNEL_ID -> default + sole allowlisted recipient.
        # BOT_USER_ID is the operator-copied ``U…`` the bridge route's self-message
        # filter needs; passed through only when set (notify / ask_user / signature
        # verification need it not). No API_BASE_URL (default = real slack.com).
        channel = os.environ["CHANNEL_SLACK_TEST_CHANNEL_ID"]
        env = {
            "CHANNEL_SLACK_BOT_TOKEN": os.environ["CHANNEL_SLACK_BOT_TOKEN"],
            "CHANNEL_SLACK_SIGNING_SECRET": os.environ["CHANNEL_SLACK_SIGNING_SECRET"],
            "CHANNEL_SLACK_REDIS_URL": res.redis_url,
            "CHANNEL_SLACK_DEFAULT_RECIPIENT": channel,
            "CHANNEL_SLACK_ALLOWED_RECIPIENTS": channel,
        }
        if os.environ.get("CHANNEL_SLACK_BOT_USER_ID"):
            env["CHANNEL_SLACK_BOT_USER_ID"] = os.environ["CHANNEL_SLACK_BOT_USER_ID"]
        return env
    return {
        "CHANNEL_SLACK_BOT_TOKEN": "xoxb-e2e-slack-token",
        "CHANNEL_SLACK_SIGNING_SECRET": secrets.token_hex(16),
        "CHANNEL_SLACK_API_BASE_URL": _require_stub(res.slack_api_base_url, "slack"),
        "CHANNEL_SLACK_REDIS_URL": res.redis_url,
        "CHANNEL_SLACK_DEFAULT_RECIPIENT": SLACK_DEFAULT_RECIPIENT,
        "CHANNEL_SLACK_ALLOWED_RECIPIENTS": ",".join(SLACK_ALLOWED_RECIPIENTS),
    }


def _twilio_channel_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: TEST_TO -> default + sole allowlisted recipient; the whatsapp
        # sandbox leg boots the same stack with a ``whatsapp:``-prefixed FROM/TO (one
        # CHANNEL_TWILIO_FROM). No API_BASE_URL (default = real api.twilio.com).
        to = os.environ["CHANNEL_TWILIO_TEST_TO"]
        return {
            "CHANNEL_TWILIO_ACCOUNT_SID": os.environ["CHANNEL_TWILIO_ACCOUNT_SID"],
            "CHANNEL_TWILIO_AUTH_TOKEN": os.environ["CHANNEL_TWILIO_AUTH_TOKEN"],
            "CHANNEL_TWILIO_FROM": os.environ["CHANNEL_TWILIO_FROM"],
            "CHANNEL_TWILIO_REDIS_URL": res.redis_url,
            "CHANNEL_TWILIO_DEFAULT_RECIPIENT": to,
            "CHANNEL_TWILIO_ALLOWED_RECIPIENTS": to,
        }
    return {
        "CHANNEL_TWILIO_ACCOUNT_SID": "ACe2e00000000000000000000000000000",
        "CHANNEL_TWILIO_AUTH_TOKEN": secrets.token_hex(16),
        "CHANNEL_TWILIO_FROM": TWILIO_FROM,
        "CHANNEL_TWILIO_API_BASE_URL": _require_stub(res.twilio_api_base_url, "twilio"),
        "CHANNEL_TWILIO_REDIS_URL": res.redis_url,
        "CHANNEL_TWILIO_DEFAULT_RECIPIENT": TWILIO_DEFAULT_RECIPIENT,
        "CHANNEL_TWILIO_ALLOWED_RECIPIENTS": ",".join(TWILIO_ALLOWED_RECIPIENTS),
    }


def _web_channel_env(res: StackResources) -> dict[str, str]:
    """The web channel's env — the same on every leg: it has no vendor, so there is no
    stub base URL to point at and no real/mock split. Its own transcript store, the
    plain-http cookie relaxation (the harness serves the chat page over ``http``, and a
    ``Secure`` cookie is never stored there — every visitor would get a fresh session),
    and the limiter windows for its own public door family.

    ``/api/channels/web/*`` is a rate-limited PUBLIC family whose per-IP bucket collapses
    every harness client into one 127.0.0.1 entry, and one visitor action is several
    requests: a page load is the shell plus each bundle file it links, the stream-cap leg
    opens the per-visitor maximum at once, and the specs sharing one stack land in one
    window. The stock 120/min + 30/10s are not comfortably above that, so the windows are
    pinned high exactly as the interactions-callback family's are — the limiter stays ON,
    and no leg's determinism rests on the operator defaults. The per-visitor stream cap, the
    answer-restore cap and the POST body cap are pinned so their legs read them back off this
    env instead of restating the plugin's defaults."""
    return {
        "CHANNEL_WEB_REDIS_URL": res.redis_url,
        "CHANNEL_WEB_SESSION_COOKIE_SECURE": "false",
        "CHANNEL_WEB_MAX_STREAMS_PER_VISITOR": str(WEB_MAX_STREAMS_PER_VISITOR),
        "CHANNEL_WEB_MAX_ANSWER_RESTORES": str(WEB_MAX_ANSWER_RESTORES),
        "CHANNEL_WEB_MAX_BODY_BYTES": str(WEB_MAX_BODY_BYTES),
        "TAI_RATE_LIMIT_FAMILIES__CHANNELS_WEB__LIMIT": "100000",
        "TAI_RATE_LIMIT_FAMILIES__CHANNELS_WEB__BURST": "100000",
    }


def _require_stub(url: str | None, medium: str) -> str:
    """A MOCK medium needs its recording-stub base URL on resources (a REAL medium
    talks to the live vendor and needs none). A mock medium missing its stub is a
    mis-wired fixture, caught here rather than at boot."""
    if url is None:
        raise RuntimeError(
            f"build_channel_stack (mock {medium}) requires the {medium} stub base URL on resources; "
            "the channel_stack fixture allocates the stubs and passes them as resource_kwargs"
        )
    return url


def _channel_env(res: StackResources, variants: Variants) -> dict[str, str]:
    """The ``CHANNEL_*`` env for the channel profile: per-plugin bot credential, a random
    per-stack inbound secret, the API base URL pointed at that medium's recording stub,
    the correlation store on this stack's Redis DB, and the disjoint default/allowlist
    recipient policy. The two public-base-URL keys are filled at boot with replica B's
    origin (see ``replica_b_origin_env_keys``).

    Each of telegram / slack / twilio is independently mock-or-real (``TAI_E2E_REAL``):
    a real medium drops its stub base URL (plugin default = live vendor) and reads its
    credential + test recipient from the operator template. All-mock (default) is
    byte-for-byte today's env. web has no vendor at all, so it is always real."""
    switch = _switch()
    env = _base_env(res, variants)
    env.update(_telegram_channel_env(res, real=switch.is_real("telegram")))
    env.update(_slack_channel_env(res, real=switch.is_real("slack")))
    env.update(_twilio_channel_env(res, real=switch.is_real("twilio")))
    env.update(_web_channel_env(res))
    # Channel-loop answers forward through the interactions callback door, whose per-IP
    # rate limiter buckets all loopback traffic together. Pin its windows high so the
    # shared 127.0.0.1 bucket never trips on test volume (the limiter stays ON).
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__BURST"] = "100000"
    return env


def _channel_public_keys(switch: HarnessSettings) -> list[str]:
    """The public-base-URL env keys the channel stack routes to ``E2E_PUBLIC_BASE_URL``
    when a channel is real inbound: telegram's setWebhook origin
    (``CHANNEL_TELEGRAM_PUBLIC_BASE_URL``) when telegram is real, and the ask_user
    callback origin (``INTERACTIONS_PUBLIC_BASE_URL``) minted into a real medium's
    outbound whenever any channel is real. Empty on the all-mock default, so the
    loopback replica-B fill is unchanged."""
    keys: list[str] = []
    if switch.is_real("telegram"):
        keys.append("CHANNEL_TELEGRAM_PUBLIC_BASE_URL")
    if any(switch.is_real(seam) for seam in _CHANNEL_SEAMS):
        keys.append("INTERACTIONS_PUBLIC_BASE_URL")
    return keys


def build_channel_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS, NO backend worker — the channel-plugin cross-worker loop home.

    Loads four channel plugins so one stack exercises telegram + slack + twilio + web:
    the first three each register a channel, a signed inbound door, and (telegram) a
    setWebhook hook; web registers its channel and its PUBLIC chat doors, whose credential
    is the visitor's session cookie. Two replicas give the deterministic act-on-A /
    inbound-on-B addressing the loop needs; ``run_backend=False`` makes the module honestly
    ``backendless``, so it runs on the default backend leg only. Auth off. Carries
    ``ask_user`` and ``notify_user`` plus the interactions callback door and the
    notifications read router.

    No conversations backend here, so web's message door (which bridges through
    ``conversations.accept``) has nothing to accept into: the web round trip through that
    door is the bridge suite's, and this stack carries web's ask/answer half only."""
    manifest = {
        "default_routers": "none",
        "channel_modules": [
            "tai42_channel_telegram",
            "tai42_channel_slack",
            "tai42_channel_twilio",
            "tai42_channel_web",
        ],
        "routers_modules": [
            "tai42_skeleton.routers.health",
            "tai42_skeleton.routers.tools",
            "tai42_skeleton.routers.config",
            "tai42_skeleton.routers.interactions",
            "tai42_skeleton.routers.notifications",
        ],
        # reload_config + notify_user project via ``api_tools`` (the notifications router
        # registers the notify_user op); ask_user loads as a builtin module. So tools[]
        # carries only the interactions entry.
        "tools": [_INTERACTIONS_ENTRY],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "notify_user", "reload_config"],
    }
    switch = _switch()
    return StackConfig(
        name="channel",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=_channel_env(res, variants),
        run_backend=False,
        run_metrics=False,
        auth=False,
        # Filled at boot with replica B's origin: the ask minted on A carries a callback
        # URL that resolves on B, and telegram's setWebhook URL points at B. Known only
        # once ports bind. A real inbound channel overrides its key to the public origin
        # (see ``public_base_url_env_keys``), so the vendor reaches it.
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL", "CHANNEL_TELEGRAM_PUBLIC_BASE_URL"],
        public_base_url_env_keys=_channel_public_keys(switch),
        public_base_url=switch.public_base_url,
    )


# ---- messaging-bridge profile -------------------------------------------
#
# The bridge routes an inbound message (authed API caller or channel adapter) to an agent
# turn whose answer is durably stored and delivered back. The identities below are the
# fixed coordinates the bridge suite binds routes to and synthesizes inbound against.

# Twilio deployment numbers (a channel route's ``our_identity`` = the number we are texted
# at). A and B are two numbers under the one fake account — the multi-identity leg.
BRIDGE_TWILIO_ACCOUNT_SID = "ACe2ebridge000000000000000000000000"
BRIDGE_TWILIO_FROM = "+15550100001"
BRIDGE_TWILIO_FROM_B = "+15550100002"
# The human on the far end of a twilio conversation (the ``client_address``). Doubles as the
# ask_user default recipient so a pending ask and a bridge turn share one number pair.
BRIDGE_TWILIO_CLIENT = "+15559001111"
BRIDGE_TWILIO_CLIENT_B = "+15559002222"

# WhatsApp phone_number_ids (a channel route's ``our_identity``).
BRIDGE_WHATSAPP_PHONE_ID = "111000111000111"
BRIDGE_WHATSAPP_PHONE_ID_B = "222000222000222"
BRIDGE_WHATSAPP_PHONE_ID_C = "333000333000333"
# The human wa_id on the far end (the ``client_address``); allowlisted so an ask_user over
# whatsapp can deliver to it.
BRIDGE_WHATSAPP_CLIENT = "15559003333"

# Cost-cap bounds: max concurrent turns per worker; max turns per client_address per hour.
BRIDGE_MAX_CONCURRENT_TURNS = 4
BRIDGE_PER_ADDRESS_TURNS_PER_HOUR = 5


def _bridge_twilio_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: TEST_TO -> default + sole allowlisted recipient; no API_BASE_URL
        # (plugin default = real api.twilio.com).
        to = os.environ["CHANNEL_TWILIO_TEST_TO"]
        return {
            "CHANNEL_TWILIO_ACCOUNT_SID": os.environ["CHANNEL_TWILIO_ACCOUNT_SID"],
            "CHANNEL_TWILIO_AUTH_TOKEN": os.environ["CHANNEL_TWILIO_AUTH_TOKEN"],
            "CHANNEL_TWILIO_FROM": os.environ["CHANNEL_TWILIO_FROM"],
            "CHANNEL_TWILIO_REDIS_URL": res.redis_url,
            "CHANNEL_TWILIO_DEFAULT_RECIPIENT": to,
            "CHANNEL_TWILIO_ALLOWED_RECIPIENTS": to,
        }
    return {
        "CHANNEL_TWILIO_ACCOUNT_SID": BRIDGE_TWILIO_ACCOUNT_SID,
        "CHANNEL_TWILIO_AUTH_TOKEN": secrets.token_hex(16),
        "CHANNEL_TWILIO_FROM": BRIDGE_TWILIO_FROM,
        "CHANNEL_TWILIO_API_BASE_URL": _require_stub(res.twilio_api_base_url, "twilio"),
        "CHANNEL_TWILIO_REDIS_URL": res.redis_url,
        "CHANNEL_TWILIO_DEFAULT_RECIPIENT": BRIDGE_TWILIO_CLIENT,
        "CHANNEL_TWILIO_ALLOWED_RECIPIENTS": ",".join([BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_CLIENT_B]),
    }


def _bridge_whatsapp_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: DEFAULT_PHONE_NUMBER_ID is our_identity, TEST_TO the allowlisted
        # wa_id; the VERIFY_TOKEN is operator-set (shared with Meta's dashboard
        # subscription). No API_BASE_URL (plugin default = real graph.facebook.com).
        return {
            "CHANNEL_WHATSAPP_ACCESS_TOKEN": os.environ["CHANNEL_WHATSAPP_ACCESS_TOKEN"],
            "CHANNEL_WHATSAPP_APP_SECRET": os.environ["CHANNEL_WHATSAPP_APP_SECRET"],
            "CHANNEL_WHATSAPP_VERIFY_TOKEN": os.environ["CHANNEL_WHATSAPP_VERIFY_TOKEN"],
            "CHANNEL_WHATSAPP_REDIS_URL": res.redis_url,
            "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID": os.environ["CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"],
            "CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS": os.environ["CHANNEL_WHATSAPP_TEST_TO"],
        }
    return {
        "CHANNEL_WHATSAPP_ACCESS_TOKEN": "e2e-whatsapp-access-token",
        "CHANNEL_WHATSAPP_APP_SECRET": secrets.token_hex(16),
        "CHANNEL_WHATSAPP_VERIFY_TOKEN": secrets.token_hex(16),
        "CHANNEL_WHATSAPP_API_BASE_URL": _require_stub(res.whatsapp_api_base_url, "whatsapp"),
        "CHANNEL_WHATSAPP_REDIS_URL": res.redis_url,
        "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID": BRIDGE_WHATSAPP_PHONE_ID,
        "CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS": BRIDGE_WHATSAPP_CLIENT,
    }


def _bridge_channel_env(res: StackResources) -> dict[str, str]:
    """The twilio + whatsapp + web ``CHANNEL_*`` env for the bridge profile: per-plugin
    credential, a random per-stack inbound secret, the API base URL pointed at that medium's
    recording stub, the correlation store on this stack's Redis DB, and the ask_user
    recipient policy (a default twilio recipient; an allowlisted whatsapp wa_id).

    twilio / whatsapp are independently mock-or-real (``TAI_E2E_REAL``): a real medium
    drops its stub base URL (plugin default = live vendor) and reads its credential +
    test recipient from the operator template. All-mock (default) is byte-for-byte
    today's env. web has no vendor at all, so it is always real."""
    switch = _switch()
    env = _bridge_twilio_env(res, real=switch.is_real("twilio"))
    env.update(_bridge_whatsapp_env(res, real=switch.is_real("whatsapp")))
    env.update(_web_channel_env(res))
    return env


def build_bridge_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + backend + metrics, access control ON — the messaging-bridge home.

    Carries the redis conversations backend (``CONVERSATIONS_REDIS_URL``), the memory
    checkpoint provider (conversation continuity lives in the serve worker that ran the
    turn, so a spec pins its inbound fires to one replica), the twilio + whatsapp + web
    channel plugins (twilio/whatsapp outbound pointed at their in-process stubs; web has no
    vendor — its public chat page, message door and SSE stream ARE the medium), and the
    ``tools_agent`` + ``deep_agent`` agents on the scripted LLM stub. Access control is ON so
    the API door resolves a caller principal and the turn runs AS a route's bound execution
    key; the ``bridge_stack`` fixture seeds the root key + the public-channel-door route
    table before boot."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        "channel_modules": ["tai42_channel_twilio", "tai42_channel_whatsapp", "tai42_channel_web"],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.conversations",
            "tai42_skeleton.routers.checkpoints",
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.agents",
            "tai42_skeleton.routers.notifications",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
            # The pairing-code mint builtin, opted in as a tool[].module row exactly as a
            # deployment does: the bridge suite drives it as a tool-target route to prove the
            # R8 {code, expires_at} contract end to end.
            {"title": "builtin-pairing", "module": "tai42_skeleton.tools.builtin.get_pairing_code"},
        ],
        "agents": [
            {"title": "tai-agents-tools", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]},
            {"title": "tai-agents-deep", "module": "tai42_agents.deep_agent", "include": ["deep_agent"]},
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        # notify_user rides the mounted notifications router (projected via api_tools): the
        # bridge suite drives it for the whatsapp media/template and recipient-policy legs.
        "user_tools": ["ask_user", "notify_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    env["CONVERSATIONS_REDIS_URL"] = res.redis_url
    env.update(_memory_agent_state_env())
    env.update(_llm_env(res))
    env.update(_bridge_channel_env(res))
    # Small delivery ceiling + backoff so an undeliverable answer reaches terminal ``failed`` fast.
    env["CONVERSATIONS_DELIVERY_MAX_ATTEMPTS"] = "2"
    env["CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS"] = "1"
    env["CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS"] = "1"
    # Cost caps pinned low: global in-flight-turn ceiling and per-address per-hour turn rate.
    env["CONVERSATIONS_MAX_CONCURRENT_TURNS"] = str(BRIDGE_MAX_CONCURRENT_TURNS)
    env["CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR"] = str(BRIDGE_PER_ADDRESS_TURNS_PER_HOUR)
    # Loopback callbacks share one 127.0.0.1 bucket; pin the limiter windows high so test
    # volume never trips it.
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__BURST"] = "100000"
    switch = _switch()
    # A real inbound channel (twilio/whatsapp) mints the ask_user callback into its
    # outbound over the public origin instead of replica-B loopback; empty on all-mock.
    bridge_public_keys = (
        ["INTERACTIONS_PUBLIC_BASE_URL"] if any(switch.is_real(s) for s in ("twilio", "whatsapp")) else []
    )
    return StackConfig(
        name="bridge",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"],
        public_base_url_env_keys=bridge_public_keys,
        public_base_url=switch.public_base_url,
    )


# ---- Stripe payments profile --------------------------------------------
#
# The payments leg drives the full external-pay loop: a money-pinned preset over the
# ``create_stripe_checkout_ask_external`` composed tool opens an external ask, a signed
# ``checkout.session.completed`` reaches the topic, the bridge answers the blocked ask,
# and a reconciliation run recovers a webhook the platform never heard about. Modelled on
# ``build_bridge_stack``: REPLICAS + access control genuinely ON, no worker (Test B calls
# ``reconcile_stripe_payments`` directly over authed MCP, so nothing schedules work).

# The bridge/door shared-secret verifier the composed ask binds per question: header-based
# (NOT ``post_only``), reading ``TAI_BRIDGE_CALLBACK_SECRET`` at the door. Author-bound as
# the ask_external extension's ``config.verifier``, so an agent can never supply it.
_STRIPE_ASK_VERIFIER = {
    "name": "shared_secret",
    "config": {"header": "X-TAI-Bridge-Secret", "secret_env": "TAI_BRIDGE_CALLBACK_SECRET"},
}

# A dummy Stripe secret key. The ``sk_test_`` prefix is load-bearing: ``_expected_livemode``
# reads it and it must agree with the FakeStripe stub's ``livemode: false`` on every session.
_STRIPE_TEST_SECRET_KEY = "sk_test_e2e0000000000000000000000000"


def build_payments_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS, access control ON, NO backend/metrics — the Stripe payments home.

    Loads the ``stripe`` webhook verifier (canonical ``webhook_verifier_modules``), the
    built-in ``shared_secret`` verifier (the composed ask's per-question binding resolves
    it), the identity provider (access control resolves a caller principal), the
    ``ask_external`` extension, and all three stripe tool modules — the builder composed
    with ``ask_external``, plus the bridge and the reconciler for the recovery leg. The
    ``payments_stack`` fixture seeds a root key + the public webhook/callback route table
    before boot, and the FakeStripe stub's origin, the two secrets and the api key ride in
    as resources.

    ``INTERACTIONS_PUBLIC_BASE_URL`` is filled at boot with replica B's origin so the
    callback URL the platform mints is DIALABLE by the bridge running inside the stack (the
    bridge's SSRF pin compares against this same value). The callback rate-limit windows are
    pinned high: the webhook loop, the forged-session rejection, and a reconciliation run
    that re-answers everything all share one 127.0.0.1 bucket."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module, "tai42_skeleton.webhooks.builtin.shared_secret"],
        "webhook_verifier_modules": ["tai42_webhook_verifier_stripe"],
        # The api_keys router mounts POST /api/auth/api-keys: the hook's execution key must be
        # a minted key's user_id (every mint stamps the fingerprint the bind resolves), so the
        # payments leg mints one and binds it. _CORE_ROUTERS carries hooks/interactions/presets.
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.api_keys"],
        "extensions_modules": ["tai42_skeleton.extensions.builtin.ask_external"],
        "storage_module": variants.storage.module,
        "tools": [
            # The builder, composed with ask_external into create_stripe_checkout_ask_external:
            # the author-bound verifier is the combo element's config, out of the agent's reach.
            {
                "title": "stripe-checkout",
                "module": "tai42_tools_stripe.tools.create_stripe_checkout",
                "extensions": {
                    "create_stripe_checkout": [[{"name": "ask_external", "config": {"verifier": _STRIPE_ASK_VERIFIER}}]]
                },
            },
            # The hook's bridge target and the recovery-layer reconciler: registered so the
            # hook and an authed reconcile call can each resolve them by name.
            {"title": "stripe-confirm", "module": "tai42_tools_stripe.tools.confirm_stripe_payment"},
            {"title": "stripe-reconcile", "module": "tai42_tools_stripe.tools.reconcile_stripe_payments"},
            # The flexible-amount payment-link builder — a fire-and-return hosted link with no
            # ask/callback. It mints a Checkout Session through the SAME client seam as the
            # checkout builder, so the FakeStripe stub answers it and no run-time call reaches
            # a real Stripe host. Kept off every user/agent surface (see ``user_tools`` below).
            {"title": "stripe-payment-link", "module": "tai42_tools_stripe.tools.create_stripe_payment_link"},
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        # The four stripe names are kept off the user surface (they are answer capabilities);
        # agents are given the money-pinned preset over the composed tool, never these.
        "user_tools": ["ask_user", "reload_config"],
    }
    switch = _switch()
    stripe_real = switch.is_real("stripe")
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    # Stripe tool config. MOCK: the api base points at the in-process FakeStripe stub and
    # the test-mode key agrees with the stub's livemode. REAL: drop the api base (plugin
    # default = real api.stripe.com) and read the operator's ``sk_test_`` key.
    if stripe_real:
        env["STRIPE_SECRET_KEY"] = os.environ["STRIPE_SECRET_KEY"]
    else:
        if res.stripe_stub_base is not None:
            env["STRIPE_API_BASE"] = res.stripe_stub_base
        env["STRIPE_SECRET_KEY"] = _STRIPE_TEST_SECRET_KEY
    # The topic's stripe verifier reads this env name; MOCK signs deliveries with the
    # harness-minted secret, REAL feeds the dashboard endpoint's ``whsec_`` signing secret.
    if stripe_real:
        env["E2E_STRIPE_WEBHOOK_SECRET"] = os.environ["STRIPE_WEBHOOK_SECRET"]
    elif res.stripe_webhook_secret is not None:
        env["E2E_STRIPE_WEBHOOK_SECRET"] = res.stripe_webhook_secret
    # One value read by BOTH the door's shared_secret verifier and the bridge tool.
    if res.bridge_callback_secret is not None:
        env["TAI_BRIDGE_CALLBACK_SECRET"] = res.bridge_callback_secret
    # Loopback callbacks share one 127.0.0.1 bucket; pin the limiter windows high so the
    # webhook loop + forged rejection + reconciliation volume never trips it.
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__BURST"] = "100000"
    return StackConfig(
        name="payments",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=False,
        run_metrics=False,
        auth=True,
        # Filled at boot with replica B's origin: the callback URL minted on A is dialable
        # by the bridge, and the SSRF pin's ground truth is this same value. Real Stripe
        # delivers ``checkout.session.completed`` to the public origin, so the callback the
        # bridge answers is minted there instead of loopback.
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"],
        public_base_url_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"] if stripe_real else [],
        public_base_url=switch.public_base_url,
    )


def build_schedule_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + backend + metrics carrying the ``schedule_task`` probe branch and
    the backend's scheduler process — the home of the scheduling spec.

    Held separate from the reload-heavy ``replicas`` profile because on celery a
    ``schedule_task`` tool riding preset reload churn can leave the prefork pool unable
    to dispatch. The split is not "scheduling never meets reload": the scheduling spec
    here drives a fleet reload while the schedule is live and asserts it survives, so the
    supported ``schedule_task`` + reload topology is covered on every backend leg. The
    boot engine spawns the scheduler process (``extra_backend_processes``) here."""
    manifest = {
        "default_routers": "none",
        # The checkpoints router projects sweep_checkpoints as a tool so the
        # schedulable-sweep leg has a real tool to schedule via run_tool_schedule_task.
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.checkpoints"],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True, with_schedule_branch=True),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env.update(_memory_agent_state_env())
    return StackConfig(
        name="schedule",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=False,
    )


def build_auth_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS with access control ON: the pluggable identity provider (records
    in its own store) + the Postgres policy store."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        # A deliver-only stub channel (registers on import, mounts no route) so the
        # isolation suite can drive a channel-delivered ask_user — the ticket-contained
        # mode where the callback URL rides the channel — and pin the add-frame carries
        # no ticket.
        "channel_modules": ["tai42_e2e_fixtures.stub_channel"],
        # The login router mounts the always-public claim-exchange door (POST
        # /api/login/claim) the owned-key onboarding leg exchanges against; the
        # notifications router mounts the internal sink's read/send doors the isolation
        # suite drives to prove per-identity audience filtering.
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.login",
            "tai42_skeleton.routers.notifications",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    # Pin BOTH rate-limit windows: exercise the 10-second burst window (L=10),
    # keep the per-minute window high enough that it can never trip first.
    env["TAI_RATE_LIMIT_FAMILIES__UNIVERSAL_WEBHOOK__BURST"] = "10"
    env["TAI_RATE_LIMIT_FAMILIES__UNIVERSAL_WEBHOOK__LIMIT"] = "1000"
    # Small recent-runs / notifications windows so the owned-key completeness pins can
    # overflow the shared window with a handful of records within the suite timeout (the
    # per-identity index/feed must still return the addressed identity's own record).
    env["TAI_TOOL_RUNS_RECENT_RUNS_LIMIT"] = "3"
    env["INTERACTIONS_NOTIFICATIONS_FEED_MAX"] = "5"
    # A channel-delivered ask_user mints a callback ticket + URL from the public base URL,
    # so this setting is required; the host is never dialed, but it must be an https value.
    env["INTERACTIONS_PUBLIC_BASE_URL"] = "https://e2e.local"
    return StackConfig(
        name="auth",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
    )


# The accounts stack's known first-owner bootstrap token. Pinning a known value drives
# the gated bootstrap path deterministically (the auto-token is logged only by the
# SET-NX winner, so a spec could not read it) while still exercising the gate. The
# auto-token SET-NX convergence rests on the plugin's own unit tests.
_ACCOUNTS_BOOTSTRAP_TOKEN = "e2e-accounts-bootstrap-token"

# The oidc stack's login provider and the coordinates the two OIDC members share with
# the in-process signing issuer. ``_OIDC_CLIENT_ID`` must equal the ``OAuthIdp``'s
# construction client (the ``aud`` it stamps into id_tokens), which ``accounts-oidc``
# verifies; ``_OIDC_MACHINE_AUDIENCE`` is the audience ``identity-oidc`` requires on
# issuer-minted machine JWTs.
_OIDC_PROVIDER_NAME = "e2e"
_OIDC_CLIENT_ID = "e2e-client"
_OIDC_CLIENT_SECRET = "e2e-secret"
_OIDC_STATE_KEY = "e2e-oidc-state-key"
_OIDC_MACHINE_AUDIENCE = "e2e-machine"


def build_accounts_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS with access control ON, the Postgres accounts provider alongside
    the redis key provider.

    ``accounts-postgres`` owns password login, opaque ``tai-sess-`` sessions, and
    invites in its own ``accounts_*`` tables (applied into the e2e template DB, so
    the per-stack clone carries them); ``redis`` keeps ``sk-`` API keys validatable
    on the same deployment. The public ``/api/login`` aggregator + the plugin's
    login/users routers are mounted, plus the authed ``/api/system/kinds`` door.
    Seeded with a root key (``seed_auth=True``) so a spec can compare key-auth and
    session-auth against one stack. The first-owner bootstrap token is pinned to a
    known value (see ``_ACCOUNTS_BOOTSTRAP_TOKEN``)."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": ["tai42_identity_redis", "tai42_accounts_postgres"],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.login",
            "tai42_skeleton.routers.system_kinds",
            "tai42_accounts_postgres.routes_login",
            "tai42_accounts_postgres.routes_users",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    # Ordered resolution: the accounts provider claims its own session tokens, the
    # redis provider claims sk- keys; a non-matching provider is a MISS, not an error.
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", "redis"])
    # The access-control policy store binds to the ``default`` database (skeleton
    # component); the accounts plugin binds to the SAME clone (its component's binding
    # also defaults to ``default``), the template carrying both schemas — both resolve
    # through the default database _base_env already declares; no per-store PG env here.
    env["TAI_ACCOUNTS_BOOTSTRAP_TOKEN"] = _ACCOUNTS_BOOTSTRAP_TOKEN
    # The plugin's rate-limit counters + bootstrap token ride the same ACCESS_CONTROL_REDIS_URL
    # the identity-provider factory receives; sessions live in Postgres, so no plugin Redis
    # env exists. /api/login needs no path/pattern env — its always-public prefix makes the
    # login namespace public code-side.
    return StackConfig(
        name="accounts",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
    )


def build_oidc_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The accounts stack plus the two OIDC members, both pointed at the in-process
    signing issuer (``netfixtures.OAuthIdp``).

    ``accounts-oidc`` adds browser-less OIDC login (authorize -> issuer -> callback
    mints a ``tai-sess-`` session, subjects namespaced ``oidc:{provider}:{sub}``);
    ``identity-oidc`` validates issuer-minted machine JWTs (subjects namespaced
    ``idp:{issuer}:{sub}``). ``TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL`` is filled at boot
    with replica B's own origin (loopback ``http`` is accepted for e2e), so a login
    spec drives the flow against replica B; ``TAI_IDENTITY_OIDC_AUDIENCE`` is the
    audience the issuer stamps into machine JWTs a spec mints for replica B.

    The ``oidc`` seam swaps the in-process issuer for a real Auth0 tenant (HARNESS-MAP:
    ``AUTH0_*`` -> a ``TAI_ACCOUNTS_OIDC_PROVIDERS`` row ``preset:"auth0"`` +
    ``TAI_IDENTITY_OIDC_ISSUER`` / ``_AUDIENCE``); the ``github-login`` seam ADDS a real
    ``preset:"github"`` provider row (fed from ``GITHUB_LOGIN_*`` — real-only, no mock
    issuer exists). Both are inbound, so the login redirect origin routes to the public
    base URL. All-mock (default) is byte-for-byte today's in-process-issuer wiring."""
    from dataclasses import replace

    switch = _switch()
    oidc_real = switch.is_real("oidc")
    gh_real = switch.is_real("github-login")

    if not oidc_real and res.oidc_issuer_base_url is None:
        raise RuntimeError("build_oidc_stack requires resources.oidc_issuer_base_url (the signing OIDC issuer origin)")

    base = build_accounts_stack(res, variants)
    manifest = {**base.manifest}
    manifest["lifecycle_modules"] = [*base.manifest["lifecycle_modules"], "tai42_accounts_oidc", "tai42_identity_oidc"]
    manifest["routers_modules"] = [*base.manifest["routers_modules"], "tai42_accounts_oidc.routes"]
    env = {**base.env}
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", "accounts-oidc", "identity-oidc", "redis"])
    # accounts-oidc login provider row(s). MOCK: one row whose issuer is the in-process
    # IdP (client_id is the IdP's construction client — the id_token ``aud`` the callback
    # verifies; the secret is a fixture value the stub IdP never checks). REAL oidc: a real
    # Auth0 row (preset fills the label; the operator supplies the per-tenant issuer).
    providers: list[dict] = []
    if oidc_real:
        providers.append(
            {
                "name": "auth0",
                "preset": "auth0",
                "issuer": os.environ["AUTH0_ISSUER"],
                "client_id": os.environ["AUTH0_CLIENT_ID"],
                "client_secret": os.environ["AUTH0_CLIENT_SECRET"],
                "claim": "sub",
            }
        )
        # identity-oidc validates machine JWTs against the same real issuer + API audience.
        env["TAI_IDENTITY_OIDC_ISSUER"] = os.environ["AUTH0_ISSUER"]
        env["TAI_IDENTITY_OIDC_AUDIENCE"] = os.environ["AUTH0_AUDIENCE"]
    else:
        # The guard above raised unless the in-process issuer origin is present here.
        assert res.oidc_issuer_base_url is not None
        providers.append(
            {
                "name": _OIDC_PROVIDER_NAME,
                "issuer": res.oidc_issuer_base_url,
                "client_id": _OIDC_CLIENT_ID,
                "client_secret": _OIDC_CLIENT_SECRET,
                "claim": "sub",
            }
        )
        # identity-oidc: validate-only, same issuer, the machine-JWT audience. RS256 is
        # the default allowed alg (the issuer signs RS256); the subject claim is ``sub``.
        env["TAI_IDENTITY_OIDC_ISSUER"] = res.oidc_issuer_base_url
        env["TAI_IDENTITY_OIDC_AUDIENCE"] = _OIDC_MACHINE_AUDIENCE
    if gh_real:
        # A real GitHub OAuth app via the plain-OAuth2 ``github`` preset (fixed endpoints,
        # no discovery/id_token). Real-only — the in-process issuer has no github mode.
        providers.append(
            {
                "name": "github",
                "preset": "github",
                "client_id": os.environ["GITHUB_LOGIN_CLIENT_ID"],
                "client_secret": os.environ["GITHUB_LOGIN_CLIENT_SECRET"],
            }
        )
    env["TAI_ACCOUNTS_OIDC_PROVIDERS"] = json.dumps(providers)
    env["TAI_ACCOUNTS_OIDC_STATE_KEY"] = _OIDC_STATE_KEY
    # A real login provider registers its OAuth redirect at the public origin, so the
    # login base URL routes there instead of replica-B loopback; empty on all-mock.
    oidc_public_keys = ["TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL"] if (oidc_real or gh_real) else []
    return replace(
        base,
        name="oidc",
        manifest=manifest,
        env=env,
        replica_b_origin_env_keys=["TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL"],
        public_base_url_env_keys=oidc_public_keys,
        public_base_url=switch.public_base_url,
    )


def build_agents_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1) + metrics — the LLM->tool->LLM loop over a scripted stub.

    Loads the whole reference agents package (``_AGENT_ENTRIES``): every agent
    runs on the scripted LLM/embedding stub + local stack resources with the
    in-process ``memory`` checkpoint/store provider."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.agents"],
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "agents": _AGENT_ENTRIES,
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env.update(_memory_agent_state_env())
    env.update(_llm_env(res))
    return StackConfig(
        name="agents",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_agents_redis_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + metrics — the production-default langgraph ``redis``
    checkpoint/store provider on the module-capable checkpoint Redis.

    Two ``--workers 1`` masters on two ports give deterministic A-then-B
    addressing (MULTIWORKER load-balances one port and cannot target a specific
    worker), so the cross-worker resume test can checkpoint a thread via replica A
    and resume it via replica B. ``tools_agent`` exercises the checkpoint resume
    seam (it wires a checkpointer, no store); ``retrieval_tools_agent`` exercises
    the redis STORE round-trip (it embeds into and searches the langgraph store).
    The whole-package coverage lives on the memory-provider ``build_agents_stack``."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.agents"],
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "agents": [
            {"title": "tai-agents-tools", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]},
            {
                "title": "tai-agents-retrieval",
                "module": "tai42_agents.retrieval_tools_agent",
                "include": ["retrieval_tools_agent"],
            },
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env.update(_redis_agent_state_env(res))
    env.update(_llm_env(res))
    return StackConfig(
        name="agents-redis",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_studio_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(2) + backend + metrics, access control ON, serving the REAL
    built Studio through the skeleton — the browser-e2e profile. One app port
    (the browser origin: the SPA and ``/api`` share it). Loads the selected identity
    provider, the Postgres accounts provider (password login + opaque ``tai-sess-``
    sessions), the github webhook verifier, and the fixture OAuth connector provider.

    Served surface — ``default_routers="all"``: the skeleton mounts its whole
    ``DEFAULT_API_ROUTERS`` set (a curated manifest that omits one leaves its Studio page
    dark), then this manifest's extras, then the SPA catch-all last. So the browser suite
    drives every nav page against the same router set production serves. The only extras
    named here are the accounts plugin's own login + users routes.

    The accounts + redis key providers coexist so the login screen renders its password
    form and keeps the key-paste fallback; ``studio_plugins`` carries the accounts plugin
    so its users-admin page mounts into the Studio shell (its API routers alone mount no
    page). The first-owner bootstrap gate is pinned to ``_ACCOUNTS_BOOTSTRAP_TOKEN``.

    TRAP: ``/api/login``'s public-ness comes from the code-side
    ``always_public_path_prefixes`` default, not a route row or ``ACCESS_CONTROL_PATH_PATTERNS``.
    Any stack that sets ``ACCESS_CONTROL_ALWAYS_PUBLIC_PATH_PREFIXES`` REPLACES that default
    wholesale (pydantic env-list semantics) and must re-include ``/api/login``."""
    if res.studio_dist_path is None:
        raise RuntimeError("build_studio_stack requires resources.studio_dist_path (the built Studio dist)")
    manifest = {
        "default_routers": "all",
        "lifecycle_modules": [
            variants.identity.lifecycle_module,
            # Importing the accounts provider registers "accounts-postgres" in both the
            # accounts and identity registries (it answers its own tai-sess- sessions).
            # Its accounts_* tables ride the per-stack clone of the e2e template DB.
            "tai42_accounts_postgres",
            "tai42_webhook_verifier_github",
            "tai42_e2e_fixtures.connector_provider",
        ],
        # The ``"all"`` default set already mounts every core + feature router; the only
        # routers NOT in it are the accounts plugin's own routes, so they are the sole
        # extras named here. The SPA catch-all is force-appended last by the loader.
        "routers_modules": [
            "tai42_accounts_postgres.routes_login",
            "tai42_accounts_postgres.routes_users",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            _toolbox_tools_entry(),
            # The reference plugin's demo tools: studio_demo_echo (the custom tool
            # panel's subject) plus studio_demo_form/fail (auto-form fallbacks).
            {"title": "reference-plugin-tools", "module": "reference_plugin.tools"},
            # notify_user projects via ``api_tools`` (the notifications router registers
            # the op); notify_user(channel=None) records to the internal sink the
            # notifications screen renders.
            *_builtin_entries(),
        ],
        "agents": [
            {"title": "tai-agents", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]},
        ],
        # Installed studio plugins whose built ``studio/`` dists the skeleton serves via
        # the injected import map: the reference plugin and the accounts plugin (its
        # API routers alone register no Studio page, so it must be listed here).
        "studio_plugins": ["reference_plugin", "tai42_accounts_postgres"],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "notify_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    # Ordered resolution: the accounts provider claims tai-sess- tokens, the key provider
    # claims sk- keys; a non-matching provider is a MISS, not an error.
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", variants.identity.name])
    # The access-control policy store and the accounts plugin's Postgres — whose
    # accounts_* tables share this stack's database, the template carrying both
    # schemas — bind to the ``default`` database _base_env already declares; no
    # per-store PG env here.
    # First-owner bootstrap gate, pinned to a known value (see ``_ACCOUNTS_BOOTSTRAP_TOKEN``).
    env["TAI_ACCOUNTS_BOOTSTRAP_TOKEN"] = _ACCOUNTS_BOOTSTRAP_TOKEN
    # Tier one of the route mapping: request-path regex -> route template. Tier two
    # (template -> resource id) is seeded into the PG route store by
    # ``harness.seed_studio_auth`` before boot.
    env["ACCESS_CONTROL_PATH_PATTERNS"] = json.dumps(STUDIO_PATH_PATTERNS)
    env["STUDIO_DIST_PATH"] = res.studio_dist_path
    # Lift the ``root`` rate-limit family for the browser leg. Every SPA request — the
    # index shell AND every JS/CSS asset a page load fans out to — charges the single
    # ``root`` family (``/{spa_path:path}`` has no static stem). Human-paced per-IP
    # traffic never approaches the default budget, but the serial UI suite fires many
    # page loads in quick succession from ONE client bucket (the Playwright loopback),
    # and their aggregate burst occasionally trips the default root ceiling (120/10s),
    # 429-ing a critical JS chunk so the app never boots — a blank page that surfaces as
    # a 60s nav-click timeout. Lift it for the harness exactly as channels_web /
    # interactions_callback / trigger are lifted for their own harness fan-outs.
    env["TAI_RATE_LIMIT_FAMILIES__ROOT__LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_FAMILIES__ROOT__BURST"] = "100000"
    # Push the failed-MCP re-probe interval past the whole browser run. The reprobe loop holds
    # the reload gate for the duration of a probe (up to mcp_probe_timeout) on every pass, and
    # resets to its short initial interval each time a NEW failed server appears — and the
    # secret-ref / mcp specs continuously seed intentionally-failing stub servers (`/bin/true`,
    # dangling secret refs) that NEVER recover, so on CI the reprobe fires ~every 30s and keeps
    # the reload gate closed, widening every reload-gated write's 503 window past the specs'
    # retry budgets and stalling fleet-reload convergence. Re-probing test fixtures that can
    # never recover buys nothing, so defer the loop beyond the run (in production the default
    # 30s reprobe still self-heals a genuinely transient MCP outage — this is harness-only).
    # Env names carry the double ``MCP`` — ``CoreSettings`` has env_prefix ``TAI_MCP_`` and the
    # fields are ``mcp_reprobe_*`` (composed name ``TAI_MCP_MCP_REPROBE_*``, as the sibling
    # ``mcp_probe_timeout`` → ``TAI_MCP_MCP_PROBE_TIMEOUT``). A single-``MCP`` name is silently
    # dropped (settings ``extra="ignore"``) and the default 30s would stand.
    env["TAI_MCP_MCP_REPROBE_INITIAL_SECONDS"] = "3600"
    env["TAI_MCP_MCP_REPROBE_MAX_SECONDS"] = "3600"
    # The github webhook verifier reads its secret from this env var; a bound-but-unsigned
    # delivery then fails verification with a clean 401 rather than a 500.
    if res.gh_webhook_secret is not None:
        env["E2E_GH_WEBHOOK_SECRET"] = res.gh_webhook_secret
    # ask_user mints its callback ticket from a public base URL; the stack's own origin.
    env["INTERACTIONS_PUBLIC_BASE_URL"] = "https://e2e.local"
    env.update(_memory_agent_state_env())
    if res.llm_base_url is not None:
        env["LLM_BASE_URL"] = res.llm_base_url
        env["LLM_API_KEY"] = "e2e-test"
        env["LLM_MODEL"] = "e2e-scripted"
    # The connectors surface: the connector store binds to the ``default`` database
    # (skeleton component) _base_env already declares, so it is live (store-configured);
    # its Redis cache rides the shared ``_redis_feature_env``. Wire the fixture provider's
    # crypto keys + stub-IdP endpoints when the runner supplied them — mirroring
    # build_connectors_stack.
    if res.connectors_kek is not None:
        env["CONNECTORS_KEK"] = res.connectors_kek
    if res.connectors_state_hmac_key is not None:
        env["CONNECTORS_STATE_HMAC_KEY"] = res.connectors_state_hmac_key
    if res.idp_base_url is not None:
        env["E2E_IDP_BASE_URL"] = res.idp_base_url
        env["E2E_IDP_CLIENT_ID"] = "e2e-client"
        env["E2E_IDP_CLIENT_SECRET"] = "e2e-secret"
    # Optional marketplace wiring for the browser leg. The marketplace router is always
    # mounted under ``"all"``, so this block only points it at the harness-run registry;
    # with marketplace_url unset the router still answers non-404 but has no registry to
    # browse (the marketplace specs gate themselves on TAI_E2E_MARKETPLACE).
    if res.marketplace_url is not None:
        env["MARKETPLACE_URL"] = res.marketplace_url
        env["MARKETPLACE_ADVISORIES_POLL"] = "true"
        env["MARKETPLACE_ADVISORIES_INTERVAL_S"] = "1"
        # The attribution store binds to the ``default`` database (skeleton component)
        # _base_env already declares — the stack's own PG clone.
        if res.package_index_url is not None:
            env["PIP_INDEX_URL"] = f"{res.package_index_url}/simple/"  # pip's PEP 503 root on the fixture server
    return StackConfig(
        name="studio",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=2,
        run_backend=True,
        run_metrics=True,
        auth=True,
        # The OAuth connect flow signs the deployment origin and validates it against
        # CONNECTORS_REDIRECT_URI_ALLOWLIST fail-closed; the app port is only known at
        # boot, so the stack fills this with its own origin.
        origin_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
    )


def build_connectors_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS, auth off — OAuth connect + refresh-lock against the stub IdP.
    Encryption keys are random per stack; the connector provider is a fixture."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": ["tai42_e2e_fixtures.connector_provider"],
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.connectors"],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    # The connector store binds to the ``default`` database (skeleton component) _base_env
    # already declares, so it is live (store-configured); its Redis cache rides the shared
    # ``_redis_feature_env``.
    if res.connectors_kek is not None:
        env["CONNECTORS_KEK"] = res.connectors_kek
    if res.connectors_state_hmac_key is not None:
        env["CONNECTORS_STATE_HMAC_KEY"] = res.connectors_state_hmac_key
    if res.idp_base_url is not None:
        # The fixture connector provider reads these to point its OAuth endpoints +
        # client credentials at the stub IdP.
        env["E2E_IDP_BASE_URL"] = res.idp_base_url
        env["E2E_IDP_CLIENT_ID"] = "e2e-client"
        env["E2E_IDP_CLIENT_SECRET"] = "e2e-secret"
    return StackConfig(
        name="connectors",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=False,
        # The OAuth connect flow signs the deployment origin and validates it against
        # CONNECTORS_REDIRECT_URI_ALLOWLIST fail-closed; the ports are known only at boot,
        # so the stack fills this with both replicas' origins.
        origin_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
    )


# ---- shipped-connectors profile (B3) ------------------------------------
#
# The two SHIPPED OAuth connector plugins — google + atlassian — loaded as their real
# descriptor modules (NOT the fixture ``connector_provider``). The descriptors hardcode
# the vendor authorize/token URLs with no env indirection, so the leg's scope ENDS at
# descriptor registration + the locally-built authorize (launch) URL: a stub IdP cannot
# intercept the vendor token exchange, and probe launches the sub-service then calls the
# real vendor. Token/refresh/probe against live vendors is PLAN_2's real-connector leg.

# The two shipped connector descriptor modules; importing each registers its provider
# through ``tai42_app.connectors.register_connector`` (google → Gmail/Calendar/Drive,
# atlassian → Jira/Confluence/Compass).
_GOOGLE_CONNECTOR_MODULE = "tai42_connector.google.core.connector"
_ATLASSIAN_CONNECTOR_MODULE = "tai42_connector.atlassian.core.connector"

# The OAuth client credentials each shipped descriptor reads by env name
# (``client_id_env`` / ``client_secret_env``). Fixed fixture values: the client_id is
# stamped verbatim into the locally-built authorize URL the leg asserts on; the secret
# is never used (no token exchange happens on this leg), but the connect flow reads it
# so it must be present.
# The client_ids are PUBLIC: the shipped-connectors leg asserts the launch URL stamps
# them verbatim, so the spec imports these rather than re-hardcoding the literals.
GOOGLE_CLIENT_ID = "e2e-google-client-id"
_GOOGLE_CLIENT_SECRET = "e2e-google-client-secret"
ATLASSIAN_CLIENT_ID = "e2e-atlassian-client-id"
_ATLASSIAN_CLIENT_SECRET = "e2e-atlassian-client-secret"


def build_shipped_connectors_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend/metrics, auth off — the SHIPPED google + atlassian
    connector descriptors loaded and their launch (authorize) URLs asserted.

    Mounts the connectors router over a live connector store (bound to the ``default``
    database, so it is store-configured) with random per-stack crypto keys, and points
    ``CONNECTORS_<VENDOR>_CLIENT_ID/SECRET`` at fixed fixture values. The connect flow
    builds the authorize URL PURELY LOCALLY from the descriptor's hardcoded authorize
    endpoint + the client_id + this stack's own redirect origin (no network to the
    vendor), so the leg is hermetic. ``CONNECTORS_REDIRECT_URI_ALLOWLIST`` is filled at
    boot with this stack's own origin (the connect flow validates the request-derived
    redirect_uri against it fail-closed)."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [_GOOGLE_CONNECTOR_MODULE, _ATLASSIAN_CONNECTOR_MODULE],
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.connectors"],
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    # The connector store binds to the ``default`` database (skeleton component) _base_env
    # already declares, so it is live (store-configured); its Redis cache rides the shared
    # ``_redis_feature_env``.
    if res.connectors_kek is not None:
        env["CONNECTORS_KEK"] = res.connectors_kek
    if res.connectors_state_hmac_key is not None:
        env["CONNECTORS_STATE_HMAC_KEY"] = res.connectors_state_hmac_key
    # The client credentials the shipped descriptors resolve by env name (the same var
    # names the operator template supplies). MOCK: fixed fixture values — only the
    # client_id is launch-URL-bearing, the secret present-but-unused on the hermetic leg.
    # REAL: the operator's live OAuth-app credentials, so the launch URL and (on the e2e
    # host) the real consent + token exchange run against the live vendor. The consent
    # round-trip itself is real-only test behavior (no mock counterpart) and needs the
    # OAuth redirect registered at the PUBLIC origin — which the connect flow validates
    # against CONNECTORS_REDIRECT_URI_ALLOWLIST. MOCK boot-fills that key from the LOOPBACK
    # origin (``origin_allowlist_env_keys``); a real connector routes it to the PUBLIC origin
    # instead via ``public_allowlist_env_keys`` (mirrors ``public_base_url_env_keys``).
    switch = _switch()
    connectors_real = switch.is_real("connector-google") or switch.is_real("connector-atlassian")
    if switch.is_real("connector-google"):
        env["CONNECTORS_GOOGLE_CLIENT_ID"] = os.environ["CONNECTORS_GOOGLE_CLIENT_ID"]
        env["CONNECTORS_GOOGLE_CLIENT_SECRET"] = os.environ["CONNECTORS_GOOGLE_CLIENT_SECRET"]
    else:
        env["CONNECTORS_GOOGLE_CLIENT_ID"] = GOOGLE_CLIENT_ID
        env["CONNECTORS_GOOGLE_CLIENT_SECRET"] = _GOOGLE_CLIENT_SECRET
    if switch.is_real("connector-atlassian"):
        env["CONNECTORS_ATLASSIAN_CLIENT_ID"] = os.environ["CONNECTORS_ATLASSIAN_CLIENT_ID"]
        env["CONNECTORS_ATLASSIAN_CLIENT_SECRET"] = os.environ["CONNECTORS_ATLASSIAN_CLIENT_SECRET"]
    else:
        env["CONNECTORS_ATLASSIAN_CLIENT_ID"] = ATLASSIAN_CLIENT_ID
        env["CONNECTORS_ATLASSIAN_CLIENT_SECRET"] = _ATLASSIAN_CLIENT_SECRET
    return StackConfig(
        name="shipped-connectors",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
        # The connect flow signs the deployment origin and validates the request-derived
        # redirect_uri against this allowlist fail-closed; the app port is known only at
        # boot, so the stack fills it with its own origin (MOCK). A real connector routes
        # the same key to the PUBLIC origin the OAuth redirect is registered at.
        origin_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
        public_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"] if connectors_real else [],
        public_base_url=switch.public_base_url,
    )


# ---- tool-extensions profile --------------------------------------------

# Extension modules the extensions profile loads so its probe-tool branches resolve —
# startup extension-validation aborts loudly on a referenced-but-unloaded extension.
_TOOL_EXTENSION_MODULES = [
    "tai42_toolbox.extensions.batch",
    "tai42_toolbox.extensions.cache",
    "tai42_toolbox.extensions.chain",
    "tai42_toolbox.extensions.output_schema",
    "tai42_skeleton.extensions.builtin.monitor",
    "tai42_skeleton.extensions.builtin.ask_external",
]

# The JSON Schema the ``output_schema`` extension is author-bound to. ``e2e_worker_info``'s
# result satisfies it; ``e2e_echo``'s (a bare string) cannot — the validate-and-raise half.
_OUTPUT_SCHEMA_CONFIG = {
    "name": "output_schema",
    "config": {
        "schema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}},
            "required": ["pid"],
        }
    },
}


def build_extensions_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — the home of the tool-extension coverage specs
    (cache / chain / output_schema / monitor / ask_external).

    Single worker on purpose: the ``cache`` wrapper's value store is process-local, so a
    two-worker fleet could serve a repeat call from another worker's empty store and mask
    the cache. The fixture monitoring backend records each span the ``monitor`` extension
    opens onto the probe channel; the interactions router + public base URL drive the
    ``ask_external`` flow. The probe tools carry one branch per extension under test."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _TOOL_EXTENSION_MODULES,
        "monitoring_module": "tai42_e2e_fixtures.monitor_backend",
        "storage_module": variants.storage.module,
        "tools": [
            {
                "title": PROBE_TOOLS_TITLE,
                "module": "tai42_e2e_fixtures.tools",
                "extensions": {
                    # cache: a repeat identical call is served from the store, so
                    # the wrapped tool's record side effect fires only once.
                    "e2e_record": [["cache"]],
                    # chain: transform e2e_echo's output with jq into e2e_record's args;
                    # monitor: trace a standalone call as one TOOL span; output_schema:
                    # echo's string result violates the bound schema (validate-and-raise half).
                    "e2e_echo": [["chain"], ["monitor"], [_OUTPUT_SCHEMA_CONFIG], ["batch"]],
                    # output_schema: worker_info's dict result satisfies the bound
                    # schema, so its branch is the advertise-and-pass half.
                    "e2e_worker_info": [[_OUTPUT_SCHEMA_CONFIG]],
                    # ask_external: drive the human-in-the-loop external ask off the
                    # link the wrapped tool builds from the callback url.
                    "e2e_external_link": [["ask_external"]],
                },
            },
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    # ask_external opens an external-format ask_user, which mints a callback ticket from a
    # public base URL (the host is never dialed).
    env["INTERACTIONS_PUBLIC_BASE_URL"] = "https://e2e.local"
    return StackConfig(
        name="extensions",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_monitoring_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1) with the langfuse monitoring plugin registered against the
    compose-provided self-hosted Langfuse (opt-in)."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.observability"],
        # The probe entry attaches a proxy branch and (this profile) an e2e_echo monitor
        # branch, so proxy + prometheus + the monitor builtin must all load or extension
        # validation aborts boot.
        "extensions_modules": [
            "tai42_toolbox.extensions.prometheus",
            "tai42_toolbox.extensions.proxy",
            "tai42_skeleton.extensions.builtin.monitor",
        ],
        "monitoring_module": "tai42_monitoring_langfuse",
        "storage_module": variants.storage.module,
        # e2e_echo_monitor traces each standalone call as a TOOL span, giving the langfuse
        # observability test a real run to read back.
        "tools": [_probe_tools_entry(with_backend_branches=False, with_monitor_branch=True), *_builtin_entries()],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    # The langfuse monitoring module is real either way — only the host + key pair
    # change. MOCK: the compose-baked self-hosted coordinates on resources. REAL: the
    # cloud host + key pair from the operator template (``LANGFUSE_*`` read verbatim).
    if _switch().is_real("langfuse"):
        host = os.environ["LANGFUSE_HOST"]
        public_key = os.environ["LANGFUSE_PUBLIC_KEY"]
        secret_key = os.environ["LANGFUSE_SECRET_KEY"]
    else:
        if not (res.langfuse_host and res.langfuse_public_key and res.langfuse_secret_key):
            # A monitoring stack with blank credentials boots and then fails cryptically
            # inside the plugin; a missing coordinate is a mis-gated fixture, caught here.
            raise RuntimeError(
                "build_monitoring_stack requires langfuse_host + langfuse_public_key + langfuse_secret_key"
            )
        host, public_key, secret_key = res.langfuse_host, res.langfuse_public_key, res.langfuse_secret_key
    env = _base_env(res, variants)
    env["LANGFUSE_HOST"] = host
    env["LANGFUSE_PUBLIC_KEY"] = public_key
    env["LANGFUSE_SECRET_KEY"] = secret_key
    return StackConfig(
        name="monitoring",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_marketplace_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1) with the marketplace client wired at the harness-run
    registry: the marketplace router, a short advisories poll, and the package
    index the installer resolves wheels from.

    One worker so an install's manifest patch + reload is observed on a deterministic
    process (a fleet could serve the post-reload tool listing off a not-yet-reloaded
    worker). No backend, no metrics sidecar — nothing marketplace-shaped touches
    either."""
    if res.marketplace_url is None or res.package_index_url is None:
        raise RuntimeError(
            "build_marketplace_stack requires resources.marketplace_url and resources.package_index_url; "
            "the marketplace_stack fixture allocates the registry + package index and passes them as resource_kwargs"
        )
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.marketplace"],
        # The probe entry attaches a proxy branch, so the proxy extension must load
        # alongside prometheus or extension validation aborts boot.
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "storage_module": variants.storage.module,
        "tools": [_probe_tools_entry(with_backend_branches=False), *_builtin_entries()],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["MARKETPLACE_URL"] = res.marketplace_url
    env["MARKETPLACE_ADVISORIES_POLL"] = "true"
    env["MARKETPLACE_ADVISORIES_INTERVAL_S"] = "1"
    # The attribution store binds to the ``default`` database (skeleton component)
    # _base_env already declares — the stack's own per-run clone.
    # The installer shells ``sys.executable -m pip install`` inheriting the worker env,
    # so this pip knob reaches it; the PEP 503 root is /simple/. REAL marketplace-pypi
    # drops the fixture index so pip resolves the tai42 packages from real pypi.org (the
    # registry-side ingest repoint — MP_PYPI_BASE_URL / MP_GITHUB_API_BASE — lives in the
    # harness-run marketplace runner, ``marketplace.py``, outside these two files).
    if not _switch().is_real("marketplace-pypi"):
        env["PIP_INDEX_URL"] = f"{res.package_index_url}/simple/"
    return StackConfig(
        name="marketplace",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_marketplace_prefix_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The marketplace stack with a persistent plugin prefix configured
    (``TAI_PLUGINS_PREFIX``) — the restart-survival home.

    The prefix is a directory under the stack root (a sibling of ``storage/``), so
    it OUTLIVES a serve-process restart and is torn down only with the stack. With
    it set, an install lands the plugin's own distribution UNDER the prefix (never
    in the shared editable venv), and boot re-adds the prefix to ``sys.path`` so the
    plugin's tools re-import after a restart. Same single-worker, backendless shape
    as the marketplace stack it derives from."""
    from dataclasses import replace
    from pathlib import Path

    base = build_marketplace_stack(res, variants)
    prefix_dir = Path(res.storage_root).parent / "plugin-prefix"
    env = {**base.env, "TAI_PLUGINS_PREFIX": str(prefix_dir)}
    return replace(base, name="marketplace-prefix", env=env)


def build_marketplace_quarantine_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The marketplace-prefix stack with the zeta compat fixture's tool module
    already wired into the manifest — the home of the boot-quarantine spec.

    The manifest carries the installer-shaped config row
    (``{"title": <module>, "module": <module>}``) for zeta's tool module, exactly
    what an install's manifest patch persists. The spec's fixture completes the
    picture BEFORE boot: it installs the zeta wheel into the plugin prefix and
    seeds its attribution row, forging the state a core upgrade strands an
    installed plugin in — present on disk, wired in the manifest, attributed in
    the store, its declared contract range excluding the running contract."""
    from dataclasses import replace

    from tai42_e2e.marketplace import ZETA_TOOLS_MODULE

    base = build_marketplace_prefix_stack(res, variants)
    manifest = {
        **base.manifest,
        "tools": [*base.manifest["tools"], {"title": ZETA_TOOLS_MODULE, "module": ZETA_TOOLS_MODULE}],
    }
    return replace(base, name="marketplace-quarantine", manifest=manifest)


# The Studio SPA catch-all router. Its ``/{path}`` route matches every path, so it must
# import LAST — any router after it serves nothing. The marketplace merge inserts a plugin
# router before it.
_STUDIO_SPA_ROUTER = "tai42_skeleton.routers.plugins"


def build_router_merge_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The marketplace stack with the SPA catch-all router LAST — the home of the
    plugin-router/middleware auto-merge spec.

    ``default_routers="none"`` with an explicit ``routers_modules`` (the marketplace core
    surface plus the SPA catch-all last) so installing a router-providing fixture plugin
    exercises the ordering-aware merge: the installer inserts the plugin's router module
    immediately before ``tai42_skeleton.routers.plugins``, and a restart mounts it there —
    reachable because it precedes the catch-all. Same shape as the marketplace stack it
    derives from."""
    from dataclasses import replace

    base = build_marketplace_stack(res, variants)
    manifest = {**base.manifest, "default_routers": "none"}
    manifest["routers_modules"] = [*base.manifest["routers_modules"], _STUDIO_SPA_ROUTER]
    return replace(base, name="router-merge", manifest=manifest)


def build_default_router_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — boots on the DEFAULT router set
    (``default_routers="all"`` with no ``routers_modules``) so the route-coverage
    guard can assert every Studio feature route the skeleton default-mounts answers
    non-404.

    The manifest names no routers of its own: the loader mounts ``DEFAULT_API_ROUTERS``
    plus the SPA catch-all last, as a bare full-Studio deployment does. It carries the
    toolbox ``generate_uuid`` tool purely so the tool-extensions door has a tool to answer
    for; ``api_tools`` off. No backend/storage/metrics — the doors for absent providers
    still mount and answer non-404, which is the point. Auth off."""
    manifest = {
        "default_routers": "all",
        "tools": [_toolbox_tools_entry()],
        "api_tools": {"enabled": False},
    }
    return StackConfig(
        name="default-router",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_api_router_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — the HEADLESS default set (``default_routers="api"``
    with no ``routers_modules``): the loader mounts ``DEFAULT_API_ROUTERS`` but NOT the
    SPA catch-all, so ``/api/*`` answers while ``/`` (and any client path) has no SPA
    shell to serve. The boot test asserts that contrast against the ``"all"`` and
    ``"none"`` modes.

    Same bare shape as ``build_default_router_stack`` — only ``default_routers`` differs,
    the single variable under test."""
    manifest = {
        "default_routers": "api",
        "tools": [_toolbox_tools_entry()],
        "api_tools": {"enabled": False},
    }
    return StackConfig(
        name="api-router",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


# ---- operations-projection profile --------------------------------------


def _projection_manifest(variants: Variants, api_tools: dict) -> dict:
    """The shared manifest the projection-chain profiles vary only ``api_tools`` on.

    Mounts ``_CORE_ROUTERS`` plus the ``api_keys`` router so the ``/api/auth/*``
    operations register — the tier-2 (default-excluded) family the projection-chain spec
    proves absent-by-default and includable. Single worker, no backend: the projection
    surface is a per-process property that needs no fleet."""
    return {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.api_keys"],
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [_probe_tools_entry(with_backend_branches=False), _INTERACTIONS_ENTRY],
        "api_tools": api_tools,
    }


def build_projection_stack(res: StackResources, variants: Variants, *, api_tools: dict | None = None) -> StackConfig:
    """MULTIWORKER(1), no backend, auth OFF — the operations-projection profile.

    Proves the projection chain end-to-end on a real booted stack: a destructive
    projected op carries ``destructiveHint``; ``expose_destructive=false`` drops the
    destructive ops from the surface; a bad ``api_tools.include`` fails startup
    loudly; the tier-2 ``/api/auth/*`` ops are default-excluded but includable. The
    caller varies ``api_tools`` per assertion (default-enabled when unset)."""
    manifest = _projection_manifest(variants, api_tools if api_tools is not None else dict(_PROJECTED_API_TOOLS))
    return StackConfig(
        name="projection",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_projection_authz_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend, access control ON — the projection AUTHZ profile.

    The same projected surface as ``build_projection_stack`` but with the identity
    provider + Postgres policy store wired ON, so a non-privileged key dispatching a
    projected op over MCP is denied at the tool edge (a ``PermissionDenied``-backed
    ``ToolError``). The route table is seeded by ``seed_projection_authz`` before
    boot."""
    manifest = _projection_manifest(variants, dict(_PROJECTED_API_TOOLS))
    manifest["lifecycle_modules"] = [variants.identity.lifecycle_module]
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    return StackConfig(
        name="projection-authz",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=True,
    )


# ---- OFF profile (D8) ----------------------------------------------------
#
# The honest all-features-OFF deployment: no feature Redis, no default PG. Every
# DB-backed feature resolves NO store, so the whole house OFF pattern is pinned in
# one spec against one profile — 200-empty collection reads, 501 + named-code
# writes, 404 miss-identical named reads, uniform-404 public doors, an SSE 501
# before any body, /ready 200 with empty checks, a kinds off-row per feature, and
# exactly one rate-limit boot WARNING.

# The registry the marketplace search/detail/categories/kinds routes PROXY to. It
# is the registry CLIENT, not the install STORE, so it sits OUTSIDE the OFF gate: a
# store-less deployment still proxies. Pointed at a closed loopback port so the
# store-less proxy path is exercised hermetically — an unreachable registry maps to
# a 502 (UpstreamError), which proves the store being OFF does not gate the proxies
# without any outbound to the real default registry.
_OFF_UNREACHABLE_REGISTRY_URL = "http://127.0.0.1:9"


def build_off_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — the all-features-OFF profile (D8).

    Serves the WHOLE default router surface (``default_routers="all"``) so every
    gated feature's door is mounted, then subtracts the two config anchors that
    would resolve a store: the per-feature Redis URLs (``_redis_feature_env``) and
    the ``default`` database block (``_pg_env("TAI_DATABASE_DEFAULT_", res)``). With
    neither present, no DB-backed feature is configured, so each answers OFF: reads
    200-empty, writes 501 + ``<feature>-not-configured``, named reads 404
    byte-identical to a genuine miss, public doors uniform-404, the SSE stream 501
    before any body, ``/ready`` 200 with empty checks, ``GET /api/system/kinds`` an
    ``off`` row per feature, and exactly one rate-limit boot WARNING. Auth off; no
    backend, storage, or metrics — an absent provider is itself part of the OFF
    surface the doctrine covers.

    The web chat plugin is loaded here for its OWN store gate: its public doors carry a
    plugin-owned store (``CHANNEL_WEB_REDIS_URL``, falling back to the shared default)
    that this profile sets neither of, so the whole channel is switched off and every one
    of its doors — the visitor-facing page included — refuses 501 with its own code."""
    manifest = {
        "default_routers": "all",
        # generate_uuid gives the tool-run submit door a real tool to name (the OFF
        # store gate refuses the submit either way); api_tools off keeps the surface
        # to the mounted HTTP routers the doctrine is pinned against.
        "tools": [_toolbox_tools_entry()],
        "api_tools": {"enabled": False},
        "channel_modules": ["tai42_channel_web"],
    }
    env = _base_env(res, variants)
    # Subtract the two anchors that would resolve a feature store, leaving every
    # DB-backed feature genuinely unconfigured — the OFF state under test.
    for key in _redis_feature_env(res):
        env.pop(key, None)
    for key in _pg_env("TAI_DATABASE_DEFAULT_", res):
        env.pop(key, None)
    # The marketplace registry proxies are the registry CLIENT, not the install
    # store, so they are outside the OFF gate. Point them at a closed loopback port
    # so the store-less proxy path is exercised without any outbound to the real
    # default registry (an unreachable registry maps to 502, never a store refusal).
    env["MARKETPLACE_URL"] = _OFF_UNREACHABLE_REGISTRY_URL
    return StackConfig(
        name="off",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


# ---- manifest-mcp mount profile (B8) ------------------------------------
#
# The FIRST manifest-level external MCP mount (``manifest.mcp: [TaiMCPConfig]``) — no
# existing pattern to copy, ``manifests.py`` carries no ``mcp`` entries today. It mounts
# the RELEASED PyPI package ``tai42-dynamic-postgres-mcp`` by LAUNCHING its
# ``tai42-postgres-mcp`` console script as a stdio child, pointed at the harness postgres.
# This is NOT the ``/api/sub-mcp`` composition router (which only re-exposes tools already
# registered on this server) — the mounted server's tools are DISCOVERED at boot by the
# app's MCP loader (each ``manifest.mcp`` entry is probed over the pooled FastMCP client)
# and bound onto this server's own MCP surface under the entry's title prefix
# (``{title}_{tool}``, per ``binding.normalized_name``).
#
# The shipped package is a DYNAMIC/codegen MCP: its console script INTROSPECTS the live
# schema at startup (``--overwrite`` defaults ON) and generates one tool per relation per
# verb — ``<verb>_<schema>_<table>`` for select/insert/update/delete — so a table must
# pre-exist in the connected database for any tool to exist. It has NO raw ``execute_sql``
# tool. The connection is passed via its ``PG_*`` pydantic-settings env (``env_prefix`` is
# ``PG_``: ``db``/``user``/``password`` have no defaults and raise at startup if missing),
# never a ``DATABASE_URL``.

# The manifest title the mounted server's tools are prefixed with on this server's MCP
# surface (``postgres_<verb>_<schema>_<table>``).
POSTGRES_MCP_TITLE = "postgres"

# The probe relation the ``postgres_mcp_stack`` fixture seeds into the stack's Postgres
# clone BEFORE boot, so the child introspects it into a full CRUD tool set and the round
# trip reads the seeded row back through the mounted select tool. The child ALSO introspects
# the clone's skeleton/accounts tables (every one of their column types maps in the package's
# codegen), so this table is one relation among several — its generated names are the ones
# the test pins. ``schema`` + ``table`` flatten (``.`` → ``_``) into each generated tool name.
POSTGRES_MCP_PROBE_SCHEMA = "public"
POSTGRES_MCP_PROBE_TABLE = "widgets"
POSTGRES_MCP_PROBE_ROW_NAME = "b8-seed-widget"


def postgres_mcp_tool_name(verb: str) -> str:
    """The name a generated per-table CRUD tool for the probe relation binds under on THIS
    server's MCP surface: the package names each tool ``<verb>_<schema>_<table>`` and the
    mount prefixes it with the manifest title (``normalized_name`` lowercases and prefixes,
    yielding ``postgres_<verb>_<schema>_<table>``)."""
    return f"{POSTGRES_MCP_TITLE}_{verb}_{POSTGRES_MCP_PROBE_SCHEMA}_{POSTGRES_MCP_PROBE_TABLE}"


def build_postgres_mcp_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend/metrics, auth off — the shipped
    ``tai42-dynamic-postgres-mcp`` mounted as a product-level external MCP.

    The manifest's single ``mcp`` entry launches the ``tai42-postgres-mcp`` console script
    over stdio (``command`` = the absolute console-script path, so no PATH is needed in the
    child's launch env). The child is a DYNAMIC/codegen MCP: at startup it introspects the
    connected schema and generates per-table CRUD tools (``<verb>_<schema>_<table>``), which
    the app's boot-time MCP loader binds onto this server's MCP surface under the ``postgres``
    title prefix — so a test lists them over the server's own ``/mcp`` and drives one query
    round trip through the product. The ``postgres_mcp_stack`` fixture seeds a known probe
    table into the clone BEFORE boot so those tools exist to be discovered.

    The connection targets this stack's own isolated per-run Postgres clone (the same database
    the feature stores use), reached over TCP at the harness pg host/port. It is passed via the
    package's ``PG_*`` settings env (``env_prefix`` ``PG_``), never ``args`` (credentials in
    argv would show in the child's process listing). ``TOOLS_DIR`` points the child's generated
    tool modules at a per-stack directory under the stack root, off the package's shared
    ``~/.cache`` default so concurrent stacks never race one codegen dir."""
    # PG_HOST/PG_PORT/PG_DB/PG_USER/PG_PASSWORD — the exact five keys the package's
    # PostgresSettings reads (bare ``PG_`` prefix); ``_pg_env("")`` renders them verbatim.
    child_env = _pg_env("", res)
    child_env["TOOLS_DIR"] = str(Path(res.storage_root).with_name("postgres_mcp_tools"))
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        # The first-of-its-kind manifest-``mcp`` mount: exactly one transport (``command``),
        # launcher-only ``env`` alongside it (no ``args`` — the child's stdio + overwrite
        # defaults already generate every CRUD tool). Connection rides the child env, not argv.
        "mcp": [
            {
                "title": POSTGRES_MCP_TITLE,
                "config": {
                    "type": "stdio",
                    "command": venv_console_script("tai42-postgres-mcp"),
                    "env": child_env,
                },
            }
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    return StackConfig(
        name="postgres-mcp",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )
