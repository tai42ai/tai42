"""The tool-metadata overlay: the concrete Postgres store behind the tool-meta contract.

The contract is :class:`~tai42_contract.tool_meta.ToolMetaStore`.
An UNVERSIONED organizational layer over ANY tool in the namespace — folders (real
nesting entities) plus a per-tool row of ``display_name`` / ``folder_id`` / ``tags``
/ ``hidden``, keyed by tool name. The store owns the invariants the tables cannot
express (cycle-freedom, empty-folder deletes, clean-slate name reclaim).
"""

from __future__ import annotations

from tai42_contract.access_control.identity import ReadinessTarget
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_configured, component_store_settings

from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.tool_meta.store import PostgresToolMetaStore, tool_meta_store


def readiness_targets() -> list[ReadinessTarget]:
    """The tool-metadata store's database, when it is configured."""
    if not component_store_configured(SKELETON_COMPONENT):
        return []
    return [ReadinessTarget("tool_meta", PostgresClient, component_store_settings(SKELETON_COMPONENT))]


__all__ = ["PostgresToolMetaStore", "readiness_targets", "tool_meta_store"]
