"""The scheduling stack profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env, _memory_agent_state_env
from tai42_e2e.manifests.tool_entries import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _INTERACTIONS_ENTRY,
    _PROJECTED_API_TOOLS,
    _STATES_TOOLS_ENTRY,
    _probe_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


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
            _INTERACTIONS_ENTRY,
            # The state tools carry a ``schedule_task`` branch here so a scheduled fire can
            # write a subject's record — the schedule door of the state store under test.
            {**_STATES_TOOLS_ENTRY, "extensions": {"state_merge": [["schedule_task"]]}},
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
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


def build_door_schedule_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + backend + scheduler, access control ON, carrying the in-process conversation
    door tools and the ``schedule_task`` branch — the home of the keyless-fire door refusal.

    Mirrors ``build_schedule_stack`` (the proven schedule branch + scheduler on every backend, not
    reload-heavy), and adds access control ON plus the ``builtin-doors`` tools so a recurring
    schedule created with NO ``execution_key`` fires with no bound execution identity and, when its
    tool calls a conversation door in-process, the door refuses the unauthenticated run under the
    enabled gate. Seeded before boot with a root key (``seed_auth=True``); the create door is authed,
    the keyless FIRE runs in the worker with no identity."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.conversations"],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True, with_schedule_branch=True),
            _INTERACTIONS_ENTRY,
            _STATES_TOOLS_ENTRY,
            # The in-process conversation door tools (send_conversation_message / _event): a keyless
            # schedule fire calls one and the door refuses the unauthenticated run.
            {"title": "builtin-doors", "module": "tai42_skeleton.tools.builtin.doors"},
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _base_env(res, variants)
    env.update(_memory_agent_state_env())
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    env["CONVERSATIONS_REDIS_URL"] = res.redis_url
    env["CONVERSATIONS_PREFIX"] = f"{res.bus_namespace}:conversations"
    return StackConfig(
        name="door-schedule",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
    )
