"""Manifest + MCP-status operations — ``/api/manifest*``, ``/api/mcp-config*``, ``/api/mcp-status*``.

A thin skin over the live-manifest admin surface (``tai42_app.admin``), the config
manager, the reload gate, and the worker bus (``instance.app.bus``). Two groups:

Reads (each returns its shape directly; the adapter envelopes it):

* ``get_manifest`` — the PRESERVED persisted manifest's MCP section + user tools
  (``!ENV`` markers intact — the no-leak retighten; the resolved read is retired).
* ``get_manifest_preserved`` — the same preserved ``{mcp, user_tools}`` view behind the
  explicit ``/api/manifest/preserved`` door the Studio McpTab editor reads.
* ``get_mcp_config_schema`` — the JSON Schema for one MCP-config entry.
* ``get_mcp_status`` — the live MCP binding snapshot.
* ``list_failed_mcps`` — the MCP servers skipped by the viability check; a query op
  over the bus, so every worker's list arrives as its per-worker report payload.

Mutations cross the single :class:`~tai42_skeleton.config.service.ConfigService`
pipeline (validate → persist → local reload → broadcast) or, for pure runtime ops,
the shared :func:`~tai42_skeleton.operations._broadcast.broadcast` primitive. Each is
``destructive`` + ``reload_gated`` and its response embeds the per-worker fleet
report as a ``fanout`` summary:

* ``set_mcp_config`` — replace the manifest's MCP section, persist, and reload the fleet.
* ``set_mcp_secret_env`` — write a secret env value AND its ``!ENV ${KEY}`` manifest marker
  together (the combined ``apply_env_and_change`` pipeline), persist, and reload the fleet.
* ``reload_mcp`` — re-probe a single MCP server by title (all workers, or only
  ``targets``). An unknown title is a loud 404.
* ``update_manifest`` — replace the WHOLE persisted manifest and reload the whole
  fleet. Authority-changing (it governs ``api_tools`` + module loading), so it is
  tier-2 (off the default MCP surface, includable).
* ``reload_failed_mcps`` — re-probe every failed MCP server.
* ``deregister_mcp`` — detach a single MCP server's tools by title.

The one response shape every ConfigService writer returns from an
:class:`~tai42_skeleton.config.service.ApplyResult` is built by
:func:`~tai42_skeleton.operations._broadcast.apply_response`.

``_preserved_manifest_view`` is bound as a package attribute so a
``setattr(operations.manifest, "_preserved_manifest_view", …)`` takes effect: the read
doors call it THROUGH this package object at call time.
"""

from __future__ import annotations

import sys

# A reload can rebuild THIS package around a still-cached ``reads`` submodule whose module-top
# ``_pkg`` alias then points at the retired generation. Re-point it at THIS package object so
# the read doors always resolve ``_preserved_manifest_view`` from the generation that
# re-exports it — the generation a test patches at the package alias.
from . import reads
from .api_tools import update_api_tools
from .models import ManifestReplace, SetMcpSecretEnv
from .reads import (
    _preserved_manifest_view,
    get_manifest,
    get_manifest_preserved,
    get_mcp_config_schema,
    get_mcp_env_refs,
    get_mcp_status,
    list_failed_mcps,
)
from .runtime import deregister_mcp, reload_failed_mcps, reload_mcp, update_manifest
from .secret_env import set_mcp_secret_env
from .sections import (
    add_agents_entries,
    add_mcp_entries,
    add_tools_entries,
    remove_agents_entry,
    remove_mcp_entry,
    remove_tools_entry,
    set_mcp_config,
)

reads.__dict__["_pkg"] = sys.modules[__name__]

__all__ = [
    "ManifestReplace",
    "SetMcpSecretEnv",
    "_preserved_manifest_view",
    "add_agents_entries",
    "add_mcp_entries",
    "add_tools_entries",
    "deregister_mcp",
    "get_manifest",
    "get_manifest_preserved",
    "get_mcp_config_schema",
    "get_mcp_env_refs",
    "get_mcp_status",
    "list_failed_mcps",
    "reload_failed_mcps",
    "reload_mcp",
    "remove_agents_entry",
    "remove_mcp_entry",
    "remove_tools_entry",
    "set_mcp_config",
    "set_mcp_secret_env",
    "update_api_tools",
    "update_manifest",
]
