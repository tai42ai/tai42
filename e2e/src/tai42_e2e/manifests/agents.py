"""The agent stack profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.channels import _web_channel_env
from tai42_e2e.manifests.feature_env import _base_env, _llm_env, _memory_agent_state_env, _redis_agent_state_env
from tai42_e2e.manifests.tool_entries import (
    _AGENT_ENTRIES,
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


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


def build_agent_route_park_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The bridge profile on a DURABLE agent state — a conversation AGENT route whose target
    async-parks and delivers its resumed answer back through ``conversation_deliver``.

    The AGENT-direction mirror of the tool-target leg ``build_bridge_stack`` already carries.
    Two things differ from that profile and both are load-bearing:

    * the langgraph checkpoint + store move to the production ``redis`` provider on the
      module-capable checkpoint Redis, and the agents plugin's own durable park index is wired
      (``TAI_AGENTS_REDIS_URL``). An agent run is park-capable ONLY on a durable checkpoint, so
      the memory-provider bridge profile can never reach this path at all;
    * ``e2e_park_agent`` is registered — a ``tools_agent`` carrying the async-ask probe tools
      baked in. A conversation agent target is invoked as ``astream(user_message, thread_id)``,
      so a bare ``tools_agent`` route reaches no parking tool; the baked registration is what
      makes the route able to park. The park/resume/deliver machinery is the production one.

    Only the ``web`` channel is carried: its public chat page + SSE stream ARE the medium the
    delivered reply is read back on, and twilio/whatsapp add nothing to this leg. Access control
    stays ON, as on the bridge profile, so the turn runs AS the route's bound execution key.

    NO backend worker and NO metrics process, unlike the bridge profile this is otherwise shaped
    after. Every moving part of this leg is in-process: the conversations package holds no backend
    reference at all (the turn engine and the delivery machine are plain ``asyncio`` tasks), the
    web channel's chat doors are the medium, the agents park index and its resume drive ride Redis
    plus the expiry reaper, and the probe tools are entered with no backend branches. So the
    module is honestly ``backendless`` — running it under every backend variant would buy
    nothing — and the profile spawns two processes fewer per boot."""
    if res.checkpoint_redis_url is None:
        raise RuntimeError(
            "build_agent_route_park_stack requires resources.checkpoint_redis_url; allocate_resources must run "
            "with allocate_checkpoint_db=True (and TAI_E2E_CHECKPOINT_REDIS_URL must be set)"
        )
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        "channel_modules": ["tai42_channel_web.register"],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.conversations",
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.agents",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "agents": [
            {"title": "tai-agents-tools", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]},
            # The baked park-capable target the conversation route points at. It resolves its
            # ``tools_agent`` delegate per call, so both entries are needed but their order is not.
            {"title": "e2e-park-agent", "module": "tai42_e2e_fixtures.park_agent", "include": ["e2e_park_agent"]},
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    env["CONVERSATIONS_REDIS_URL"] = res.redis_url
    env["CONVERSATIONS_PREFIX"] = f"{res.bus_namespace}:conversations"
    env.update(_redis_agent_state_env(res))
    env.update(_llm_env(res))
    env.update(_web_channel_env(res))
    # The agents plugin's durable park index (reverses a parked interaction id to its parked
    # run) rides the plain feature Redis; the async ask refuses loudly without it.
    env["TAI_AGENTS_REDIS_URL"] = res.redis_url
    # Small delivery ceiling + backoff so an undeliverable answer reaches terminal fast.
    env["CONVERSATIONS_DELIVERY_MAX_ATTEMPTS"] = "2"
    env["CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS"] = "1"
    env["CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS"] = "1"
    return StackConfig(
        name="agent-route-park",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=False,
        run_metrics=False,
        auth=True,
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"],
    )


def build_agent_async_park_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + redis checkpoint, no backend — the AGENT async ``ask_user`` park lifecycle.

    The agents-plugin counterpart to ``build_async_park_stack``: a real ``tools_agent`` run
    parks on an async ``ask_user`` (raised by the ``e2e_agent_async_ask`` probe tool the model
    calls) and is resumed on the OTHER replica — by an answer through B's ``/answer`` door or by
    the 1s expiry reaper — through the agents plugin's own durable park index. The langgraph
    checkpoint is the production-default ``redis`` provider on the module-capable checkpoint
    Redis, so the paused graph crosses the worker boundary; the park index rides the plain
    feature Redis (``TAI_AGENTS_REDIS_URL``). Loading ``tools_agent`` alone registers the hidden
    ``agent_resume`` continuation the park fires (any park-capable agent module does through the
    shared park machinery), and its simpler loop drives the run. Auth off, so the probe tool
    binds the synthetic identity ``ask_user`` needs."""
    if res.checkpoint_redis_url is None:
        raise RuntimeError(
            "build_agent_async_park_stack requires resources.checkpoint_redis_url; allocate_resources must run "
            "with allocate_checkpoint_db=True (and TAI_E2E_CHECKPOINT_REDIS_URL must be set)"
        )
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.agents"],
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "agents": [
            {"title": "tai-agents-tools", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]},
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env.update(_redis_agent_state_env(res))
    env.update(_llm_env(res))
    # The agents plugin's own durable park index (reverses a parked interaction id to its
    # parked run) rides the plain feature Redis; the async ask refuses loudly without it.
    env["TAI_AGENTS_REDIS_URL"] = res.redis_url
    # An async ask_user park has no blocking waiter, so the expiry leg only resumes when the
    # reaper trips; pin its cadence low so it resumes in seconds rather than on the 30s default.
    env["INTERACTIONS_EXPIRY_REAPER_INTERVAL_SECONDS"] = "1"
    return StackConfig(
        name="agent-async-park",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )
