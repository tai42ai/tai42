"""The serve-memory stack profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env
from tai42_e2e.manifests.tool_entries import PROBE_TOOLS_TITLE
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# How many jobs a compute-pool child of the memory stack runs before the pool replaces it.
# Low enough that every child is replaced inside the memory suite's load, high enough that a
# child holding the memory of its large inputs crosses the suite's per-child bound before its
# first replacement could clear it (the suite's module docstring derives both sides).
MEMORY_WORKER_MAX_TASKS = 128

# The allocator environment a container image or unit file sets for the serve's processes:
# jemalloc preloaded by its soname, with background threads that return freed pages to the
# kernel while a process is idle. Every process of the stack inherits it.
MEMORY_ALLOCATOR_PRELOAD = "libjemalloc.so.2"
MEMORY_ALLOCATOR_CONF = "background_thread:true"


def build_memory_stack(res: StackResources, variants: Variants) -> StackConfig:
    """Two uvicorn workers, each with the default two compute-pool children, no backend and
    no metrics process; the chain tool extension branches ``e2e_document`` so a request runs
    a jq program over a large document inside the serve. The compute-pool children are
    replaced after ``MEMORY_WORKER_MAX_TASKS`` jobs, so a run of a few hundred calls crosses a
    replacement in every child. Every process runs on the deployment's allocator environment
    (``MEMORY_ALLOCATOR_PRELOAD``, ``MEMORY_ALLOCATOR_CONF``).

    Nothing the load does not exercise is loaded (no metrics route, monitoring, storage or
    builtin tools), so it does not sit in the sampled memory: the health router answers boot
    readiness, the tools router carries the run-tool door."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [
            "tai42_skeleton.routers.health",
            "tai42_skeleton.routers.tools",
        ],
        "extensions_modules": ["tai42_toolbox.extensions.chain"],
        "tools": [
            {
                "title": PROBE_TOOLS_TITLE,
                "module": "tai42_e2e_fixtures.tools",
                "extensions": {"e2e_document": [["chain"]]},
            }
        ],
        # The run-tool route is mounted by its router module alone; projection off keeps the
        # mounted operations from projecting as MCP tools.
        "api_tools": {"enabled": False},
    }
    env = _base_env(res, variants)
    # ``COMPUTE_WORKERS`` stays at the deployment default: that pool size is what the suite holds
    # to its bound.
    env["COMPUTE_WORKER_MAX_TASKS"] = str(MEMORY_WORKER_MAX_TASKS)
    env["LD_PRELOAD"] = MEMORY_ALLOCATOR_PRELOAD
    env["MALLOC_CONF"] = MEMORY_ALLOCATOR_CONF
    return StackConfig(
        name="memory",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=2,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )
