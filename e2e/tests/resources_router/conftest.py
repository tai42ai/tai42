"""B6 — the resources-router behavioral profile.

The route-coverage guard only proves ``/api/resources/get`` is MOUNTED
(``tests/routing/test_default_router_coverage.py``); this profile boots a stack
that mounts the resources router over a real storage provider so a spec can store
a resource through the storage surface and read it back through the resources door.

Local stack-profile module: the builder reuses the shared manifest primitives from
``tai42_e2e.manifests`` (the ``_CORE_ROUTERS`` set, the probe/builtin tool entries,
the base feature env) rather than duplicating them, and adds only the resources
router — the one surface this leg exercises."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _base_env,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack, Topology
from tai42_e2e.variants import Variants

# This profile runs no backend worker, so it exercises no backend seam.
pytestmark = pytest.mark.backendless


def build_resources_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — the ``_CORE_ROUTERS`` surface (which carries the
    storage router) plus the resources router, over the selected storage provider.

    A stored object written through ``/api/storage/resources`` is loadable by the
    resources door (``get_resource_by_id`` -> ``resource_manager.load_file``), so one
    stack backs both the write and the read-back the behavioral spec asserts."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.resources"],
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    return StackConfig(
        name="resources",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


@pytest.fixture(scope="module")
def resources_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from boot_stack(infra, tmp_path_factory.mktemp("resources"), build_resources_stack)
