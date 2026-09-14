"""The API-projection stack profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env
from tai42_e2e.manifests.tool_entries import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _INTERACTIONS_ENTRY,
    _PROJECTED_API_TOOLS,
    _probe_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


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
