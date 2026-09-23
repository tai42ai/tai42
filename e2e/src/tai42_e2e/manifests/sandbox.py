"""The sandbox stack profiles."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import (
    _base_env,
    _llm_env,
    _memory_agent_state_env,
    _redis_agent_state_env,
    _switch,
)
from tai42_e2e.manifests.tool_entries import _CORE_ROUTERS, _PROJECTED_API_TOOLS, _builtin_entries, _probe_tools_entry
from tai42_e2e.settings import LLM_PROVIDERS
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# The scalar ``sandbox_module`` slots: the process-based fake (every deterministic sandbox
# suite) and the REAL direct/host provider (the sandbox-local suites). A stack names EXACTLY
# ONE — the sandbox slot is a scalar single-instance holder, never both.
_SANDBOX_FAKE_MODULE = "tai42_e2e_fixtures.sandbox_provider"


_SANDBOX_LOCAL_MODULE = "tai42_sandbox_local.provider"


# The ``sandbox_exec`` base tool (a preset target the sandbox doors drive), opted in as a
# ``tools[].module`` row exactly as a deployment does.
_SANDBOX_EXEC_ENTRY = {"title": "builtin-sandbox-exec", "module": "tai42_skeleton.tools.builtin.sandbox_exec"}


# A digest-pinned stub image reference: inert under the fake/local providers (the host is the
# execution environment), but both agent plugins reject a bare tag, so a digest is required.
_STUB_SESSION_IMAGE = "img@sha256:" + "0" * 64


# The two agent-implementation entries (their vendor names ride ONLY the agent ids). Each is a
# package whose import registers the agent's run tool under its id.
_CLAUDE_AGENT_ENTRY = {"title": "tai-agents-claude", "module": "tai42_agents.claude_code", "include": ["claude_code"]}


_DEEP_AGENT_ENTRY = {
    "title": "tai-agents-deep",
    "module": "tai42_agents.langchain_deep_agent",
    "include": ["langchain_deep_agent"],
}


def build_sandbox_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — the deterministic sandbox-kind surface over the fake provider.

    Bare core plus the fake ``sandbox_module`` and, in ``tools``, the ``e2e_sandbox_probe`` pack
    and the ``sandbox_exec`` base tool — the two consumer doors the sandbox-kind / sessions /
    ``sandbox_exec`` / policy suites drive. No agents entry: the sandbox surface is
    backend-invariant, so this is the single-worker home the probe's process-level session
    registry needs (every probe call lands in the same server process)."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "sandbox_module": _SANDBOX_FAKE_MODULE,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            _SANDBOX_EXEC_ENTRY,
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    return StackConfig(
        name="sandbox",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def _sandbox_local_env(res: StackResources, variants: Variants) -> dict[str, str]:
    """The base env for a ``sandbox-local`` stack: the direct provider's host workspace ROOT on
    a per-stack dir, and the operator policy pinned at the floor the host provider can satisfy —
    isolation ``none`` and egress ``egress``. A tighter floor makes the provider reject every
    create (the isolation/egress rejection legs assert exactly that)."""
    env = _base_env(res, variants)
    env["SANDBOX_LOCAL_ROOT"] = os.path.join(res.storage_root, "sandbox-local")
    env["TAI_MCP_SANDBOX_ISOLATION"] = "none"
    env["TAI_MCP_SANDBOX_EGRESS"] = "egress"
    return env


def build_sandbox_local_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The ``build_sandbox_stack`` shape wiring the REAL direct/host provider instead of the fake.

    Host-subprocess exec over host-dir workspaces (no docker, no container image). The direct
    provider satisfies isolation ``none`` only and network ``egress`` only, so the operator
    policy sits at that floor for the run/durability legs; the isolation/egress suites drive a
    tighter floor to assert the loud capability mismatch."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "sandbox_module": _SANDBOX_LOCAL_MODULE,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            _SANDBOX_EXEC_ENTRY,
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    return StackConfig(
        name="sandbox-local",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_sandbox_local_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_sandbox_local_deep_stack(res: StackResources, variants: Variants) -> StackConfig:
    """``build_sandbox_local_stack`` plus the ``langchain_deep_agent`` entry — the deep agent runs
    end to end under the direct/host provider (its ``SandboxBackend`` reaches the same sandbox
    contract), proving it is provider-agnostic. Deterministic: needs no extra host runtime."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.agents"],
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "sandbox_module": _SANDBOX_LOCAL_MODULE,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            _SANDBOX_EXEC_ENTRY,
            *_builtin_entries(),
        ],
        "agents": [_DEEP_AGENT_ENTRY],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _sandbox_local_env(res, variants)
    env.update(_memory_agent_state_env())
    env.update(_llm_env(res))
    env["TAI_AGENTS_LANGCHAIN_DEEP_SESSION_IMAGE"] = _STUB_SESSION_IMAGE
    return StackConfig(
        name="sandbox-local-deep",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_sandbox_local_claude_stack(res: StackResources, variants: Variants) -> StackConfig:
    """``build_sandbox_local_stack`` plus the ``claude_code`` entry — the direct-mode claude leg,
    GATED on the creds host (direct ``claude_code`` assumes the operator-installed
    ``claude-agent-sdk`` wheel + the model credential on the host). Maps the single ``.env``
    ``ANTHROPIC_API_KEY`` onto the operator auth env and runs the real materialized payload."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.agents"],
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "sandbox_module": _SANDBOX_LOCAL_MODULE,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            _SANDBOX_EXEC_ENTRY,
            *_builtin_entries(),
        ],
        "agents": [_CLAUDE_AGENT_ENTRY],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _sandbox_local_env(res, variants)
    env.update(_memory_agent_state_env())
    env.update(_llm_env(res))
    env["TAI_AGENTS_CLAUDE_SESSION_IMAGE"] = _STUB_SESSION_IMAGE
    # The direct-mode leg runs only on the creds host: the adapter materializes the real
    # ``tai_runner`` into the host-dir workspace and drives the operator-installed SDK against the
    # real key mapped onto the operator auth env (no fake-runner seam — that is the fake provider's).
    if _switch().is_real("claude_agent"):
        env["TAI_AGENTS_CLAUDE_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    else:
        # Off the creds host the leg is skipped at collection; a placeholder key keeps the exactly-
        # one-auth validator happy so the stack still boots for the non-gated sibling suites.
        env["TAI_AGENTS_CLAUDE_API_KEY"] = "e2e-fake-claude-key"
    return StackConfig(
        name="sandbox-local-claude",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_claude_agent_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — the ``claude_code`` agent over the process-based fake sandbox.

    The ``build_agents_stack`` shape (agents router + memory checkpoint + scripted LLM stub) PLUS
    the fake ``sandbox_module``, the ``claude_code`` agent entry, and the fixture monitoring backend
    (for the attribution/cost leg). A scripted runner is selected per test via ``SANDBOX_FAKE_RUNNER``;
    the probe tools are registered so a ``tool_names`` bridge can proxy a real platform tool. The
    ``_llm_env`` mock wiring stays for consistency though the SDK never runs in stub mode (the runner
    stub replaces it). API-key auth by default; a test overrides ``TAI_AGENTS_CLAUDE_OAUTH_TOKEN`` /
    unsets the key per-test to exercise the OAuth mode. Auth toggled per test (off for the run/tools
    legs)."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.agents"],
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "sandbox_module": _SANDBOX_FAKE_MODULE,
        "monitoring_module": "tai42_e2e_fixtures.monitor_backend",
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            _SANDBOX_EXEC_ENTRY,
            *_builtin_entries(),
        ],
        "agents": [_CLAUDE_AGENT_ENTRY],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _base_env(res, variants)
    env.update(_memory_agent_state_env())
    env.update(_llm_env(res))
    # The fixture monitoring backend records the attribution wrap + the SDK cost emission only
    # while it reports an active trace; name the opt-in so both fire on this stack.
    env["E2E_MONITOR_ACTIVE_TRACE"] = "1"
    # The metered API-key auth mode (the OAuth mode is exercised per-test); a stub digest image.
    env["TAI_AGENTS_CLAUDE_API_KEY"] = "e2e-fake-claude-key"
    env["TAI_AGENTS_CLAUDE_SESSION_IMAGE"] = _STUB_SESSION_IMAGE
    # REAL leg: map the single .env ANTHROPIC_API_KEY onto the operator auth env and run the actual
    # materialized payload (real claude-agent-sdk) instead of the scripted stub.
    if _switch().is_real("claude_agent"):
        env["TAI_AGENTS_CLAUDE_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
        env["SANDBOX_FAKE_RUNNER"] = "real"
    return StackConfig(
        name="claude-agent",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_deep_agent_durable_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + redis checkpoint + fake persistent sandbox — the ``langchain_deep_agent`` durable
    parity home.

    The ``build_agent_async_park_stack`` shape (two deterministically-addressable workers + the
    module-capable checkpoint Redis + the ``TAI_AGENTS_REDIS_*`` park store) PLUS the fake
    ``sandbox_module`` and the ``langchain_deep_agent`` entry. REPLICAS + persistent workspace is
    what lets the durable-parity suite prove a workspace survives across turns AND a cross-worker
    resume re-attaches the SAME volume, with the per-workspace Redis lease serializing two workers.
    REAL leg: the deep agent's one model turn reaches Anthropic server-side through ``get_llm_async``
    keyed off the single ``.env`` ``ANTHROPIC_API_KEY`` (the embeddings group stays on the stub)."""
    if res.checkpoint_redis_url is None:
        raise RuntimeError(
            "build_deep_agent_durable_stack requires resources.checkpoint_redis_url; allocate_resources must run "
            "with allocate_checkpoint_db=True (and TAI_E2E_CHECKPOINT_REDIS_URL must be set)"
        )
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.agents"],
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "sandbox_module": _SANDBOX_FAKE_MODULE,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            _SANDBOX_EXEC_ENTRY,
            *_builtin_entries(),
        ],
        "agents": [_DEEP_AGENT_ENTRY],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _base_env(res, variants)
    env.update(_redis_agent_state_env(res))
    env["TAI_AGENTS_LANGCHAIN_DEEP_SESSION_IMAGE"] = _STUB_SESSION_IMAGE
    # The agents plugin's durable park index rides the plain feature Redis; the async ask refuses
    # loudly without it, and the per-workspace lease serializes the two replicas over it.
    env["TAI_AGENTS_REDIS_URL"] = res.redis_url
    # An async ask park has no blocking waiter, so its expiry leg only resumes when the reaper
    # trips; pin its cadence low so it resumes in seconds rather than on the 30s default.
    env["INTERACTIONS_EXPIRY_REAPER_INTERVAL_SECONDS"] = "1"
    # The scripted-stub model wiring (both groups on the stub) is the default; the REAL leg below
    # repoints ONLY the LLM group at anthropic and leaves the embeddings group on the stub.
    env.update(_llm_env(res))
    # REAL leg: the deep agent's one model turn reaches Anthropic server-side via get_llm_async
    # keyed off the single .env ANTHROPIC_API_KEY (the model cred never enters the session).
    if _switch().is_real("claude_agent"):
        provider = LLM_PROVIDERS["anthropic"]
        env.pop("LLM_BASE_URL", None)
        env["LLM_PROVIDER_LLM"] = provider.provider
        env["LLM_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
        env["LLM_MODEL"] = os.environ.get("REAL_E2E_LLM_MODEL", provider.default_llm_model)
    return StackConfig(
        name="deep-agent-durable",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )
