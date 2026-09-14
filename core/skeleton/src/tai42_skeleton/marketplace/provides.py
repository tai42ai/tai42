"""Classify a spec's ``provides`` items by their :data:`KIND_MANIFEST_BINDINGS`
mode and the manifest pointer they bind into.

Pure spec introspection — no I/O, no app handle — so a flow can ask which items
wire into a manifest field, which are env-selected, which write an ``mcp`` entry,
and which manifest field the data items bind into.
"""

from __future__ import annotations

from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, PluginItem, PluginSpec


def has_manifest_provides(spec: PluginSpec) -> bool:
    """Whether the spec provides any item that wires into a manifest field (a
    non-env-selected item).

    Such a plugin's live registration must be converged by an uninstall reload
    even when the manifest is already stripped — a prior partial run may have
    persisted the stripped manifest but failed the deregister reload, so the tools
    stay live until a re-run reloads.
    """
    for item in spec.provides:
        binding = KIND_MANIFEST_BINDINGS.get(item.kind)
        if binding is not None and binding.mode != "env_selected":
            return True
    return False


def env_selected_items(spec: PluginSpec) -> list[PluginItem]:
    """The spec's provides items that wire into no manifest field (the
    env-selected ``config`` kind) — pip install/uninstall is their whole
    registration."""
    items: list[PluginItem] = []
    for item in spec.provides:
        binding = KIND_MANIFEST_BINDINGS.get(item.kind)
        if binding is not None and binding.mode == "env_selected":
            items.append(item)
    return items


def mcp_entry_items(spec: PluginSpec) -> list[PluginItem]:
    """The spec's provides items that write an ``mcp`` entry (an mcp-server item
    carrying transport config, no module). A spec never mixes mcp-server with any
    other kind, so a non-empty result means this whole install is an mcp-server one."""
    items: list[PluginItem] = []
    for item in spec.provides:
        binding = KIND_MANIFEST_BINDINGS.get(item.kind)
        if binding is not None and binding.mode == "mcp_entry":
            items.append(item)
    return items


def env_manifest_pointer(spec: PluginSpec) -> str:
    """The manifest field(s) the spec's DATA items bind into (``mcp`` / ``connectors``),
    joined — the pointer named in a combined-op orphan report. A spec never mixes
    mcp-server with other kinds and every connector item binds to ``connectors``, so this
    is a single field in practice; ``manifest`` when the spec has no data item (never on
    the env-accepting path)."""
    fields: list[str] = []
    for item in spec.provides:
        binding = KIND_MANIFEST_BINDINGS.get(item.kind)
        if binding is not None and binding.payload == "data" and binding.field and binding.field not in fields:
            fields.append(binding.field)
    return ",".join(fields) if fields else "manifest"
