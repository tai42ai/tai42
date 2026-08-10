"""B5 support — the checkpoint-provider axis stack builder and conn-string helper.

A bare-name sibling module (the suite's convention for shared spec helpers), reached
by both this dir's ``conftest`` and its spec module. Reuses the shared manifest
primitives from ``tai42_e2e.manifests`` and sets only the three ``LLM_PROVIDER_*``
checkpoint knobs (``core/kit/src/tai42_kit/llm/settings.py``), so the profile
diverges from the others in exactly the axis under test — the DB-backed checkpoint
provider and its positive idle TTL."""

from __future__ import annotations

from pathlib import Path

from tai42_e2e.manifests import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _base_env,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.stack import StackConfig, StackResources, Topology
from tai42_e2e.variants import Variants

# A positive idle-TTL in minutes (``checkpoint_ttl_minutes`` must be > 0). The spec
# backdates a thread's newest ts well past this and keeps a fresh thread inside it.
CHECKPOINT_TTL_MINUTES = 60


def checkpoint_conn_string(provider: str, res: StackResources) -> str:
    """The ``LLM_PROVIDER_CHECKPOINT_CONN_STRING`` for a provider, pointed at THIS
    stack's isolated store: the per-stack Postgres clone (a libpq DSN) or a sqlite
    file under the stack root (a path both the serve worker and the harness reach)."""
    if provider == "postgres":
        return f"postgresql://{res.pg_user}:{res.pg_password}@{res.pg_host}:{res.pg_port}/{res.pg_db}"
    if provider == "sqlite":
        # A sibling of storage/ under the stack root: on shared local disk, so the
        # serve worker's saver and the harness-side saver open the same database file.
        return str(Path(res.storage_root).parent / "checkpoints.sqlite")
    raise ValueError(f"unsupported checkpoint provider for the axis leg: {provider!r}")


def build_checkpoint_stack(res: StackResources, variants: Variants, *, provider: str) -> StackConfig:
    """MULTIWORKER(1), no backend — the ``_CORE_ROUTERS`` surface plus the checkpoints
    router (so ``sweep_checkpoints`` projects as a tool and ``POST /api/checkpoints/sweep``
    is mounted), pinned to a DB-backed checkpoint provider with a positive idle TTL."""
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
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["LLM_PROVIDER_CHECKPOINT"] = provider
    env["LLM_PROVIDER_CHECKPOINT_CONN_STRING"] = checkpoint_conn_string(provider, res)
    env["LLM_PROVIDER_CHECKPOINT_TTL_MINUTES"] = str(CHECKPOINT_TTL_MINUTES)
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
