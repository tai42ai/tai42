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
