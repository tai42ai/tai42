"""The served paths of the core doors another module addresses, spelled once.

Each router registers its door on the constant here, and every module that addresses the
door (the access-control projection, the execution-key fire, the tool projection tiers, the
studio plugin registry) imports the same constant. A route-less helper: importing it
registers nothing, so a reader never mounts a router as a side effect of reading its path.
"""

from __future__ import annotations

# The access-control administration surface the api-keys, roles and policy doors serve under.
AUTH_API_PREFIX = "/api/auth"

# The per-agent run door.
AGENT_RUNS_TEMPLATE = "/api/agents/{name}/runs"

# The synchronous tool-run door: reaching it reaches every registry tool.
RUN_TOOL_PATH = "/api/run-tool"

# The background tool-run door (submit with ``POST``, list with ``GET``): reaching its submit
# reaches every registry tool.
TOOL_RUNS_PATH = "/api/tool-runs"

# The public asset door a studio plugin's files are served under (``{name}`` the plugin
# package); the import-map integrity keys are built on it.
STUDIO_ASSET_PREFIX = "/api/plugins/{name}/studio/"


def agent_run_path(name: str) -> str:
    """The run-door path of agent ``name``."""
    return AGENT_RUNS_TEMPLATE.format(name=name)
