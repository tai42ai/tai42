"""The checkpoint-provider stack builder and conn-string helper.

A bare-name sibling module (the suite's convention for shared spec helpers), reached
by both this dir's ``conftest`` and its spec module. Reuses the shared manifest
primitives from ``tai42_e2e.manifests`` and sets only the ``LLM_PROVIDER_*`` checkpoint
knobs (``core/kit/src/tai42_kit/llm/settings.py``), so the profile diverges from the
others in exactly the dimension under test — the checkpoint provider and its two
retention horizons."""

from __future__ import annotations

from pathlib import Path

from tai42_e2e import StackConfig, StackResources, Topology
from tai42_e2e.manifests import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _base_env,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.variants import Variants

# The two retention horizons, in minutes. The spec backdates a thread's newest ts — and
# a finished mark — well past them, and keeps a fresh thread inside them.
WAITING_MINUTES = 60
FINISHED_MINUTES = 30


def checkpoint_conn_string(provider: str, res: StackResources) -> str:
    """The ``LLM_PROVIDER_CHECKPOINT_CONN_STRING`` for a provider, pointed at THIS
    stack's isolated store: the per-stack Postgres clone (a libpq DSN), a sqlite file
    under the stack root (a path both the serve worker and the harness reach), or the
    stack's logical DB on the module-capable checkpoint Redis."""
    if provider == "redis":
        if res.checkpoint_redis_url is None:
            raise ValueError("the redis checkpoint provider needs the stack's checkpoint Redis DB")
        return res.checkpoint_redis_url
    if provider == "postgres":
        return f"postgresql://{res.pg_user}:{res.pg_password}@{res.pg_host}:{res.pg_port}/{res.pg_db}"
    if provider == "sqlite":
        # A sibling of storage/ under the stack root: on shared local disk, so the
        # serve worker's saver and the harness-side saver open the same database file.
        return str(Path(res.storage_root).parent / "checkpoints.sqlite")
    raise ValueError(f"unsupported checkpoint provider: {provider!r}")


def build_checkpoint_stack(res: StackResources, variants: Variants, *, provider: str) -> StackConfig:
    """MULTIWORKER(1), no backend — the ``_CORE_ROUTERS`` surface plus the checkpoints
    router (so ``sweep_checkpoints`` projects as a tool and ``POST /api/checkpoints/sweep``
    is mounted), pinned to a checkpoint provider and the two retention horizons."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.checkpoints"],
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _base_env(res, variants)
    env["LLM_PROVIDER_CHECKPOINT"] = provider
    env["LLM_PROVIDER_CHECKPOINT_CONN_STRING"] = checkpoint_conn_string(provider, res)
    env["LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES"] = str(WAITING_MINUTES)
    env["LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES"] = str(FINISHED_MINUTES)
    return StackConfig(
        name=f"checkpoint-{provider}",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )
