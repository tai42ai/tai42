"""The kept hook-run stack profiles: the webhook and trigger doors, the hook-run doors, the operator feed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env
from tai42_e2e.manifests.tool_entries import _CORE_ROUTERS, _EXTENSION_MODULES, _probe_tools_entry
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


def _hook_runs_manifest() -> dict:
    """The core routers plus the notifications doors the operator feed is read through, the
    builtin ``shared_secret`` verifier, and the probe tools; no backend and no projection."""
    return {
        "default_routers": "none",
        "lifecycle_modules": ["tai42_skeleton.webhooks.builtin.shared_secret"],
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.notifications"],
        "extensions_modules": _EXTENSION_MODULES,
        "tools": [_probe_tools_entry(with_backend_branches=False)],
        "api_tools": {"enabled": False},
    }


def build_hook_runs_stack(res: StackResources, variants: Variants) -> StackConfig:
    """One worker on one port, no backend, auth off: the restart, shutdown and config-apply home
    of the kept hook runs, with every store on Redis (the hooks, the interactions and the tool runs)."""
    return StackConfig(
        name="hook-runs",
        topology=Topology.MULTIWORKER,
        manifest=_hook_runs_manifest(),
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_hook_runs_replicas_stack(res: StackResources, variants: Variants) -> StackConfig:
    """Two one-worker replicas on two ports, no backend, auth off: the reclaim home of the kept
    hook runs, one replica's sweep taking over the run of a killed one."""
    return StackConfig(
        name="hook-runs-replicas",
        topology=Topology.REPLICAS,
        manifest=_hook_runs_manifest(),
        env=_base_env(res, variants),
        run_backend=False,
        run_metrics=False,
        auth=False,
    )
