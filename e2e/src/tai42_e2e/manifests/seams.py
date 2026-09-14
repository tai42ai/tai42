"""The platform-seams stack profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env, _memory_agent_state_env, _pg_env
from tai42_e2e.manifests.tool_entries import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _SEAMS_TOOLS_ENTRY,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


def build_seams_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1) + backend — the platform-seams home carrying the seams fixture
    (preset seed + rename referee + invocation probe).

    Backend + the ``schedule_task`` probe branch give the rename-integrity legs a live
    SCHEDULE and the schedule/hook platform referees to block a rename on; the full core
    routers mount the presets / referees / rename / schedules / hooks doors. Single worker
    keeps the in-process referee + invocation-seam reads coherent. Auth off."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True, with_schedule_branch=True),
            _SEAMS_TOOLS_ENTRY,
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env.update(_memory_agent_state_env())
    return StackConfig(
        name="seams",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=True,
        run_metrics=False,
        auth=False,
    )


def _seams_seed_manifest() -> dict:
    """The lean store-backed seams manifest for the seed-lifecycle legs: no backend, the
    presets / tool_meta / config doors, and the seams fixture whose seed a boot applies."""
    return {
        "default_routers": "none",
        "routers_modules": [
            "tai42_skeleton.routers.health",
            "tai42_skeleton.routers.tools",
            "tai42_skeleton.routers.config",
            "tai42_skeleton.routers.presets",
            "tai42_skeleton.routers.tool_meta",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            _SEAMS_TOOLS_ENTRY,
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }


def build_seams_seed_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend, versioned store ON — the seed-lifecycle home (first-boot
    LIVE, re-boot idempotent, content-change upgrade, operator-edit preservation). Booted
    fresh per test through ``fresh_stack`` so each leg owns its seed's global state. Auth
    off."""
    return StackConfig(
        name="seams-seed",
        topology=Topology.MULTIWORKER,
        manifest=_seams_seed_manifest(),
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_seams_seed_off_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The seed stack with the versioned store OFF: the default database block is dropped,
    so the seed applier logs a VISIBLE skip and creates nothing, and the stack still boots
    healthy. The store-off doctrine leg for declared preset seeds. Auth off."""
    env = _base_env(res, variants)
    for key in _pg_env("TAI_DATABASE_DEFAULT_", res):
        env.pop(key, None)
    return StackConfig(
        name="seams-seed-off",
        topology=Topology.MULTIWORKER,
        manifest=_seams_seed_manifest(),
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )
