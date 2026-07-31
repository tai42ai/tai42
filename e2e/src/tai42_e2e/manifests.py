"""Typed builders for the manifest + feature-env of each stack profile.

A profile is a named :class:`~tai42_e2e.stack.StackConfig`: a manifest dict (the
YAML the SUT loads) plus the feature-env map that points every ``*_REDIS_URL`` /
``*_PG_*`` / plugin setting at this stack's isolated resources. The pytest
fixtures in ``tests/conftest.py`` allocate the resources and call these."""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING

from tai42_e2e.harness import STUDIO_PATH_PATTERNS
from tai42_e2e.stack import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

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


def _redis_feature_env(res: StackResources) -> dict[str, str]:
    """Point every per-feature Redis URL at the stack's logical DB, and the
    probe-record client at DB 0."""
    # CONNECTOR_STORE_REDIS_URL is deliberately absent: any CONNECTOR_STORE_* env flips
    # the ``connectors_in_use()`` gate on, so the startup catalog refresh would stall
    # retrying an absent Postgres. Only the connectors profile sets it (redis AND pg).
    return {
        "ACCESS_CONTROL_REDIS_URL": res.redis_url,
        "INTERACTIONS_REDIS_URL": res.redis_url,
        "TAI_TOOL_RUNS_REDIS_URL": res.redis_url,
        "TAI_RATE_LIMIT_REDIS_URL": res.redis_url,
        "HOOKS_REDIS_URL": res.redis_url,
        "SUB_MCP_REDIS_URL": res.redis_url,
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


def _llm_stub_env(res: StackResources) -> dict[str, str]:
    """Point the agents' model AND embedding access at the scripted stub. The
    stub serves ``/v1/chat/completions`` and ``/v1/embeddings`` off one origin, so
    both provider groups share ``res.llm_base_url``. Empty when the stub URL is
    unset (the studio profile allocates the stub port separately)."""
    if res.llm_base_url is None:
        return {}
    return {
        "LLM_BASE_URL": res.llm_base_url,
        "LLM_API_KEY": "e2e-test",
        "LLM_MODEL": "e2e-scripted",
        "EMBEDDING_BASE_URL": res.llm_base_url,
        "EMBEDDING_API_KEY": "e2e-test",
        "EMBEDDING_MODEL": "e2e-embed",
    }


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
    """The five ``<prefix>PG_*`` keys a PostgresConnectionSettings subclass reads."""
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
    # Presets + policy history live in the versioning store.
    env.update(_pg_env("VERSIONING_STORE_", res))
    # The tool_meta overlay (folders + per-tool rows) is a default-mounted platform
    # store; point it at this stack's isolated PG clone, same as the versioning store.
    env.update(_pg_env("TOOL_META_STORE_", res))
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


def _channel_env(res: StackResources, variants: Variants) -> dict[str, str]:
    """The ``CHANNEL_*`` env for the channel profile: per-plugin bot credential, a random
    per-stack inbound secret, the API base URL pointed at that medium's recording stub,
    the correlation store on this stack's Redis DB, and the disjoint default/allowlist
    recipient policy. The two public-base-URL keys are filled at boot with replica B's
    origin (see ``replica_b_origin_env_keys``)."""
    if res.telegram_api_base_url is None or res.slack_api_base_url is None or res.twilio_api_base_url is None:
        raise RuntimeError(
            "build_channel_stack requires the three channel-stub base URLs on resources "
            "(telegram_api_base_url/slack_api_base_url/twilio_api_base_url); the channel_stack "
            "fixture allocates the stubs and passes them as resource_kwargs"
        )
    env = _base_env(res, variants)
    env.update(
        {
            # telegram
            "CHANNEL_TELEGRAM_BOT_TOKEN": "e2e-telegram-bot-token",
            "CHANNEL_TELEGRAM_WEBHOOK_SECRET": secrets.token_hex(16),
            "CHANNEL_TELEGRAM_API_BASE_URL": res.telegram_api_base_url,
            "CHANNEL_TELEGRAM_REDIS_URL": res.redis_url,
            "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT": TELEGRAM_DEFAULT_RECIPIENT,
            "CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS": ",".join(TELEGRAM_ALLOWED_RECIPIENTS),
            # slack
            "CHANNEL_SLACK_BOT_TOKEN": "xoxb-e2e-slack-token",
            "CHANNEL_SLACK_SIGNING_SECRET": secrets.token_hex(16),
            "CHANNEL_SLACK_API_BASE_URL": res.slack_api_base_url,
            "CHANNEL_SLACK_REDIS_URL": res.redis_url,
            "CHANNEL_SLACK_DEFAULT_RECIPIENT": SLACK_DEFAULT_RECIPIENT,
            "CHANNEL_SLACK_ALLOWED_RECIPIENTS": ",".join(SLACK_ALLOWED_RECIPIENTS),
            # twilio
            "CHANNEL_TWILIO_ACCOUNT_SID": "ACe2e00000000000000000000000000000",
            "CHANNEL_TWILIO_AUTH_TOKEN": secrets.token_hex(16),
            "CHANNEL_TWILIO_FROM": TWILIO_FROM,
            "CHANNEL_TWILIO_API_BASE_URL": res.twilio_api_base_url,
            "CHANNEL_TWILIO_REDIS_URL": res.redis_url,
            "CHANNEL_TWILIO_DEFAULT_RECIPIENT": TWILIO_DEFAULT_RECIPIENT,
            "CHANNEL_TWILIO_ALLOWED_RECIPIENTS": ",".join(TWILIO_ALLOWED_RECIPIENTS),
        }
    )
    # Channel-loop answers forward through the interactions callback door, whose per-IP
    # rate limiter buckets all loopback traffic together. Pin its windows high so the
    # shared 127.0.0.1 bucket never trips on test volume (the limiter stays ON).
    env["TAI_RATE_LIMIT_INTERACTIONS_CALLBACK_LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_INTERACTIONS_CALLBACK_BURST"] = "100000"
    return env


def build_channel_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS, NO backend worker — the channel-plugin cross-worker loop home.

    Loads all three channel plugins so one stack exercises telegram + slack + twilio:
    each registers its channel, its signed inbound door, and (telegram) its setWebhook
    hook. Two replicas give the deterministic act-on-A / inbound-on-B addressing the loop
    needs; ``run_backend=False`` makes the module honestly ``backendless``, so it runs on
    the default backend leg only. Auth off. Carries ``ask_user`` and ``notify_user`` plus
    the interactions callback door and the notifications read router."""
    manifest = {
        "default_routers": "none",
        "channel_modules": ["tai42_channel_telegram", "tai42_channel_slack", "tai42_channel_twilio"],
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
        # once ports bind.
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL", "CHANNEL_TELEGRAM_PUBLIC_BASE_URL"],
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


def _bridge_channel_env(res: StackResources) -> dict[str, str]:
    """The twilio + whatsapp ``CHANNEL_*`` env for the bridge profile: per-plugin
    credential, a random per-stack inbound secret, the API base URL pointed at that medium's
    recording stub, the correlation store on this stack's Redis DB, and the ask_user
    recipient policy (a default twilio recipient; an allowlisted whatsapp wa_id)."""
    if res.twilio_api_base_url is None or res.whatsapp_api_base_url is None:
        raise RuntimeError(
            "build_bridge_stack requires the twilio + whatsapp stub base URLs on resources; the bridge_stack "
            "fixture allocates the stubs and passes them as resource_kwargs"
        )
    return {
        # twilio
        "CHANNEL_TWILIO_ACCOUNT_SID": BRIDGE_TWILIO_ACCOUNT_SID,
        "CHANNEL_TWILIO_AUTH_TOKEN": secrets.token_hex(16),
        "CHANNEL_TWILIO_FROM": BRIDGE_TWILIO_FROM,
        "CHANNEL_TWILIO_API_BASE_URL": res.twilio_api_base_url,
        "CHANNEL_TWILIO_REDIS_URL": res.redis_url,
        "CHANNEL_TWILIO_DEFAULT_RECIPIENT": BRIDGE_TWILIO_CLIENT,
        "CHANNEL_TWILIO_ALLOWED_RECIPIENTS": ",".join([BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_CLIENT_B]),
        # whatsapp
        "CHANNEL_WHATSAPP_ACCESS_TOKEN": "e2e-whatsapp-access-token",
        "CHANNEL_WHATSAPP_APP_SECRET": secrets.token_hex(16),
        "CHANNEL_WHATSAPP_VERIFY_TOKEN": secrets.token_hex(16),
        "CHANNEL_WHATSAPP_API_BASE_URL": res.whatsapp_api_base_url,
        "CHANNEL_WHATSAPP_REDIS_URL": res.redis_url,
        "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID": BRIDGE_WHATSAPP_PHONE_ID,
        "CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS": BRIDGE_WHATSAPP_CLIENT,
    }


def build_bridge_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + backend + metrics, access control ON — the messaging-bridge home.

    Carries the redis conversations backend (``CONVERSATIONS_REDIS_URL``), the memory
    checkpoint provider (conversation continuity lives in the serve worker that ran the
    turn, so a spec pins its inbound fires to one replica), the twilio + whatsapp
    channel plugins (outbound pointed at their in-process stubs), and the ``tools_agent`` +
    ``deep_agent`` agents on the scripted LLM stub. Access control is ON so the API door
    resolves a caller principal and the turn runs AS a route's bound execution key; the
    ``bridge_stack`` fixture seeds the root key + the public-channel-door route table before
    boot."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        "channel_modules": ["tai42_channel_twilio", "tai42_channel_whatsapp"],
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
    env.update(_pg_env("ACCESS_CONTROL_STORE_", res))
    env["CONVERSATIONS_REDIS_URL"] = res.redis_url
    env.update(_memory_agent_state_env())
    env.update(_llm_stub_env(res))
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
    env["TAI_RATE_LIMIT_INTERACTIONS_CALLBACK_LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_INTERACTIONS_CALLBACK_BURST"] = "100000"
    return StackConfig(
        name="bridge",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"],
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
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        # The four stripe names are kept off the user surface (they are answer capabilities);
        # agents are given the money-pinned preset over the composed tool, never these.
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    env.update(_pg_env("ACCESS_CONTROL_STORE_", res))
    # Stripe tool config. The api base points at the in-process FakeStripe stub; no run-time
    # call reaches a real Stripe host. The test-mode key agrees with the stub's livemode.
    if res.stripe_stub_base is not None:
        env["STRIPE_API_BASE"] = res.stripe_stub_base
    env["STRIPE_SECRET_KEY"] = _STRIPE_TEST_SECRET_KEY
    # The topic's stripe verifier reads this; the test signs deliveries with the same value.
    if res.stripe_webhook_secret is not None:
        env["E2E_STRIPE_WEBHOOK_SECRET"] = res.stripe_webhook_secret
    # One value read by BOTH the door's shared_secret verifier and the bridge tool.
    if res.bridge_callback_secret is not None:
        env["TAI_BRIDGE_CALLBACK_SECRET"] = res.bridge_callback_secret
    # Loopback callbacks share one 127.0.0.1 bucket; pin the limiter windows high so the
    # webhook loop + forged rejection + reconciliation volume never trips it.
    env["TAI_RATE_LIMIT_INTERACTIONS_CALLBACK_LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_INTERACTIONS_CALLBACK_BURST"] = "100000"
    return StackConfig(
        name="payments",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=False,
        run_metrics=False,
        auth=True,
        # Filled at boot with replica B's origin: the callback URL minted on A is dialable
        # by the bridge, and the SSRF pin's ground truth is this same value.
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"],
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
    env.update(_pg_env("ACCESS_CONTROL_STORE_", res))
    # Pin BOTH rate-limit windows: exercise the 10-second burst window (L=10),
    # keep the per-minute window high enough that it can never trip first.
    env["TAI_RATE_LIMIT_WEBHOOK_BURST"] = "10"
    env["TAI_RATE_LIMIT_WEBHOOK_LIMIT"] = "1000"
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
    env.update(_pg_env("ACCESS_CONTROL_STORE_", res))
    # The accounts plugin's own Postgres: its accounts_* tables live in the same
    # per-stack database as the policy store (the template carries both schemas).
    env.update(_pg_env("TAI_ACCOUNTS_PG_", res))
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
    audience the issuer stamps into machine JWTs a spec mints for replica B."""
    from dataclasses import replace

    if res.oidc_issuer_base_url is None:
        raise RuntimeError("build_oidc_stack requires resources.oidc_issuer_base_url (the signing OIDC issuer origin)")

    base = build_accounts_stack(res, variants)
    manifest = {**base.manifest}
    manifest["lifecycle_modules"] = [*base.manifest["lifecycle_modules"], "tai42_accounts_oidc", "tai42_identity_oidc"]
    manifest["routers_modules"] = [*base.manifest["routers_modules"], "tai42_accounts_oidc.routes"]
    env = {**base.env}
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", "accounts-oidc", "identity-oidc", "redis"])
    # accounts-oidc: one login provider whose issuer is the in-process IdP. Its
    # client_id is the IdP's construction client (the id_token ``aud`` the callback
    # verifies); the secret is a fixture value the stub IdP never checks.
    env["TAI_ACCOUNTS_OIDC_PROVIDERS"] = json.dumps(
        [
            {
                "name": _OIDC_PROVIDER_NAME,
                "issuer": res.oidc_issuer_base_url,
                "client_id": _OIDC_CLIENT_ID,
                "client_secret": _OIDC_CLIENT_SECRET,
                "claim": "sub",
            }
        ]
    )
    env["TAI_ACCOUNTS_OIDC_STATE_KEY"] = _OIDC_STATE_KEY
    # identity-oidc: validate-only, same issuer, the machine-JWT audience. RS256 is
    # the default allowed alg (the issuer signs RS256); the subject claim is ``sub``.
    env["TAI_IDENTITY_OIDC_ISSUER"] = res.oidc_issuer_base_url
    env["TAI_IDENTITY_OIDC_AUDIENCE"] = _OIDC_MACHINE_AUDIENCE
    return replace(
        base,
        name="oidc",
        manifest=manifest,
        env=env,
        replica_b_origin_env_keys=["TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL"],
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
    env.update(_llm_stub_env(res))
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
    env.update(_llm_stub_env(res))
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
    env.update(_pg_env("ACCESS_CONTROL_STORE_", res))
    # The accounts plugin's own Postgres: its accounts_* tables live in the same
    # per-stack database as the policy store (the template carries both schemas).
    env.update(_pg_env("TAI_ACCOUNTS_PG_", res))
    # First-owner bootstrap gate, pinned to a known value (see ``_ACCOUNTS_BOOTSTRAP_TOKEN``).
    env["TAI_ACCOUNTS_BOOTSTRAP_TOKEN"] = _ACCOUNTS_BOOTSTRAP_TOKEN
    # Tier one of the route mapping: request-path regex -> route template. Tier two
    # (template -> resource id) is seeded into the PG route store by
    # ``harness.seed_studio_auth`` before boot.
    env["ACCESS_CONTROL_PATH_PATTERNS"] = json.dumps(STUDIO_PATH_PATTERNS)
    env["STUDIO_DIST_PATH"] = res.studio_dist_path
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
    # The connectors surface: point BOTH halves of the connector store at this
    # stack's isolated resources (any CONNECTOR_STORE_* env flips the connector
    # catalog refresh on, so redis and pg must travel together), and wire the
    # fixture provider's crypto keys + stub-IdP endpoints when the runner supplied
    # them — mirroring build_connectors_stack.
    env["CONNECTOR_STORE_REDIS_URL"] = res.redis_url
    env.update(_pg_env("CONNECTOR_STORE_", res))
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
        env.update(_pg_env("MARKETPLACE_STORE_", res))  # attribution store on the stack's own PG clone
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
    # The connector store's redis + pg halves must travel together (any CONNECTOR_STORE_*
    # env flips the catalog refresh on), both at this stack's isolated resources.
    env["CONNECTOR_STORE_REDIS_URL"] = res.redis_url
    env.update(_pg_env("CONNECTOR_STORE_", res))
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
    if not (res.langfuse_host and res.langfuse_public_key and res.langfuse_secret_key):
        # A monitoring stack with blank credentials boots and then fails cryptically
        # inside the plugin; a missing coordinate is a mis-gated fixture, caught here.
        raise RuntimeError("build_monitoring_stack requires langfuse_host + langfuse_public_key + langfuse_secret_key")
    env = _base_env(res, variants)
    env["LANGFUSE_HOST"] = res.langfuse_host
    env["LANGFUSE_PUBLIC_KEY"] = res.langfuse_public_key
    env["LANGFUSE_SECRET_KEY"] = res.langfuse_secret_key
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
    # The attribution store is its own PG group; without this it defaults to db "tai",
    # which the harness never creates. Point it at the stack's own per-run clone.
    env.update(_pg_env("MARKETPLACE_STORE_", res))
    # The installer shells ``sys.executable -m pip install`` inheriting the worker env,
    # so this pip knob reaches it; the PEP 503 root is /simple/.
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
    env.update(_pg_env("ACCESS_CONTROL_STORE_", res))
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
