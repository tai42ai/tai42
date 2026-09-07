"""The api-door conversation profile with access control OFF.

A stack that mounts the conversations router over a real redis conversations backend
with NO auth adapter, so the api door resolves no caller from a request. The spec pins
that the door still admits a turn — acting AS the platform's synthetic no-auth principal —
rather than refusing it for want of an authenticated caller.

Local stack-profile module: the builder reuses the shared manifest primitives from
``tai42_e2e.manifests`` (the ``_CORE_ROUTERS`` set, the probe/builtin tool entries, the
base feature env) and adds only the conversations router plus its redis backend.
"""

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
    _toolbox_tools_entry,
)
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack, Topology
from tai42_e2e.variants import Variants

# The api-door turn runs its tool target IN-PROCESS on the serve worker, so this profile
# exercises no backend seam and runs on the default leg only.
pytestmark = pytest.mark.backendless


def build_conversations_off_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend, access control OFF — the ``_CORE_ROUTERS`` surface plus
    the conversations router over a redis conversations backend.

    ``generate_uuid`` (a builtin tool) is the api route's tool target, so a sent message
    runs a real turn in-process; the door's admission is what the spec asserts."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.conversations"],
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            _toolbox_tools_entry(),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["CONVERSATIONS_REDIS_URL"] = res.redis_url
    env["CONVERSATIONS_PREFIX"] = f"{res.bus_namespace}:convacoff"
    return StackConfig(
        name="conversations-off",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


@pytest.fixture(scope="module")
def conversations_off_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from boot_stack(infra, tmp_path_factory.mktemp("conversations-off"), build_conversations_off_stack)
