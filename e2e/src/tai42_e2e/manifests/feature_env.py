"""Feature-env builders shared across the stack profiles."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from tai42_e2e.settings import HarnessSettings, real_embedding_provider, real_llm_provider
from tai42_e2e.topology import StackResources

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


def _redis_feature_env(res: StackResources) -> dict[str, str]:
    """Point every per-feature Redis URL at the stack's logical DB, and the
    probe-record client at DB 0.

    The logical DB isolates co-tenant stacks, but NOT a stack whose DB index is
    re-leased while a leaked process still holds keys under it (the bus namespace
    exists for the same reason). So every feature keyspace that a foreign process
    could reach on a shared DB — one carrying an active cross-stack mutator (the
    interactions expiry reaper) or one addressed by a collision-prone LOGICAL name
    (a hook name, a connector slug, a rate-limit identity/route, a per-tool run
    index) rather than a globally-unique token — is namespaced by the same
    per-stack id the bus uses, so two stacks can never share a key.

    Access-control is deliberately absent: its identity records are addressed by
    ``sha256(raw-token)`` with a random token minted per stack, so no foreign
    process can read or claim a live stack's credential even on a re-leased DB,
    and no background reaper sweeps the ``ac:*`` range. Namespacing it would also
    demand a change to the identity-redis provider's hardcoded reverse-key prefix
    (a public settings-Protocol surface) for no theft that unique addressing does
    not already prevent."""
    return {
        "ACCESS_CONTROL_REDIS_URL": res.redis_url,
        "INTERACTIONS_REDIS_URL": res.redis_url,
        "INTERACTIONS_KEY_PREFIX": f"{res.bus_namespace}:interactions:",
        "TAI_TOOL_RUNS_REDIS_URL": res.redis_url,
        "TAI_TOOL_RUNS_KEY_PREFIX": f"{res.bus_namespace}:tool_runs:",
        "TAI_RATE_LIMIT_REDIS_URL": res.redis_url,
        "TAI_RATE_LIMIT_KEY_PREFIX": f"{res.bus_namespace}:ratelimit:",
        "HOOKS_REDIS_URL": res.redis_url,
        "HOOKS_PREFIX": f"{res.bus_namespace}:hooks",
        "SUB_MCP_REDIS_URL": res.redis_url,
        "SUB_MCP_PREFIX": f"{res.bus_namespace}:sub_mcp",
        "CONNECTOR_STORE_REDIS_URL": res.redis_url,
        "CONNECTOR_STORE_KEY_PREFIX": f"{res.bus_namespace}:connectors:",
        "E2E_PROBE_REDIS_URL": res.probe_redis_url,
    }


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
