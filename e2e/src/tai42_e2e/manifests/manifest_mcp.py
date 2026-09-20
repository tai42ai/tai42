"""The mounted-MCP stack profiles (postgres, resilience)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tai42_e2e.binaries import venv_console_script
from tai42_e2e.manifests.feature_env import _base_env, _pg_env
from tai42_e2e.manifests.tool_entries import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# The manifest title the mounted server's tools are prefixed with on this server's MCP
# surface (``postgres_<verb>_<schema>_<table>``).
POSTGRES_MCP_TITLE = "postgres"


# The probe relation the ``postgres_mcp_stack`` fixture seeds into the stack's Postgres
# clone BEFORE boot, so the child introspects it into a full CRUD tool set and the round
# trip reads the seeded row back through the mounted select tool. The child ALSO introspects
# the clone's skeleton/accounts tables (every one of their column types maps in the package's
# codegen), so this table is one relation among several — its generated names are the ones
# the test pins. ``schema`` + ``table`` flatten (``.`` → ``_``) into each generated tool name.
POSTGRES_MCP_PROBE_SCHEMA = "public"


POSTGRES_MCP_PROBE_TABLE = "widgets"


POSTGRES_MCP_PROBE_ROW_NAME = "b8-seed-widget"


def postgres_mcp_tool_name(verb: str) -> str:
    """The name a generated per-table CRUD tool for the probe relation binds under on THIS
    server's MCP surface: the package names each tool ``<verb>_<schema>_<table>`` and the
    mount prefixes it with the manifest title (``normalized_name`` lowercases and prefixes,
    yielding ``postgres_<verb>_<schema>_<table>``)."""
    return f"{POSTGRES_MCP_TITLE}_{verb}_{POSTGRES_MCP_PROBE_SCHEMA}_{POSTGRES_MCP_PROBE_TABLE}"


def build_postgres_mcp_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend/metrics, auth off — the shipped
    ``tai42-mcp-dynamic-postgres`` mounted as a product-level external MCP.

    The manifest's single ``mcp`` entry launches the ``tai42-mcp-dynamic-postgres`` console script
    over stdio (``command`` = the absolute console-script path, so no PATH is needed in the
    child's launch env). The child is a DYNAMIC/codegen MCP: at startup it introspects the
    connected schema and generates per-table CRUD tools (``<verb>_<schema>_<table>``), which
    the app's boot-time MCP loader binds onto this server's MCP surface under the ``postgres``
    title prefix — so a test lists them over the server's own ``/mcp`` and drives one query
    round trip through the product. The ``postgres_mcp_stack`` fixture seeds a known probe
    table into the clone BEFORE boot so those tools exist to be discovered.

    The connection targets this stack's own isolated per-run Postgres clone (the same database
    the feature stores use), reached over TCP at the harness pg host/port. It is passed via the
    package's ``PG_*`` settings env (``env_prefix`` ``PG_``), never ``args`` (credentials in
    argv would show in the child's process listing). ``TOOLS_DIR`` points the child's generated
    tool modules at a per-stack directory under the stack root, off the package's shared
    ``~/.cache`` default so concurrent stacks never race one codegen dir."""
    # PG_HOST/PG_PORT/PG_DB/PG_USER/PG_PASSWORD — the exact five keys the package's
    # PostgresSettings reads (bare ``PG_`` prefix); ``_pg_env("")`` renders them verbatim.
    child_env = _pg_env("", res)
    child_env["TOOLS_DIR"] = str(Path(res.storage_root).with_name("postgres_mcp_tools"))
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        # The first-of-its-kind manifest-``mcp`` mount: exactly one transport (``command``),
        # launcher-only ``env`` alongside it (no ``args`` — the child's stdio + overwrite
        # defaults already generate every CRUD tool). Connection rides the child env, not argv.
        "mcp": [
            {
                "title": POSTGRES_MCP_TITLE,
                "config": {
                    "type": "stdio",
                    "command": venv_console_script("tai42-mcp-dynamic-postgres"),
                    "env": child_env,
                },
            }
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    return StackConfig(
        name="postgres-mcp",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


# The manifest title the managed server's tools are prefixed with on this server's MCP
# surface (``resilience_<tool>``); also the ``{title}`` the reload / status ops key on.
RESILIENCE_MCP_TITLE = "resilience"


def resilience_mcp_tool_name(name: str) -> str:
    """The name a managed-server tool binds under on THIS server's MCP surface: the mount
    prefixes each tool with the manifest title (``resilience_<tool>``)."""
    return f"{RESILIENCE_MCP_TITLE}_{name}"


def build_resilience_mcp_stack(res: StackResources, variants: Variants, *, mcp_url: str) -> StackConfig:
    """MULTIWORKER(1), no backend/metrics, auth off — the managed MCP server mounted over
    streamable-http as a product-level external MCP for the connection-resilience scenario.

    The single ``mcp`` entry points at ``mcp_url`` (the http endpoint of the test-owned
    ``managed_mcp_server`` subprocess), so the app's boot-time MCP loader discovers its tools
    (``ping`` / ``echo`` / ``reflect_env``) and binds them under the ``resilience`` title
    prefix. One worker keeps the passive dispatch health the scenario reads from
    ``GET /api/mcp-status`` in the same process that serves the tool calls."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [*_builtin_entries()],
        # Exactly one transport (``url``): a plain non-managed http MCP entry, so no
        # connector auth glue is injected — ``ping`` needs none. fastmcp infers the
        # streamable-http transport from the ``/mcp`` path.
        "mcp": [
            {
                "title": RESILIENCE_MCP_TITLE,
                "config": {"type": "http", "url": mcp_url},
            }
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    return StackConfig(
        name="mcp-resilience",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )
