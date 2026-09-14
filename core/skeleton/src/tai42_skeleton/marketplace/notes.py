"""Human-readable activation and removal notes for an install or uninstall.

Each note explains a consequence the manifest patch alone does not show — an
env-selected item installed but inactive until ``TAI_CONFIG_MODE`` selects it, an
mcp-server item mounted in the manifest, or the stored env values an uninstalled
mcp server leaves behind untouched.
"""

from __future__ import annotations

from tai42_contract.plugins import PluginSpec
from tai42_kit.utils.data.env_markers import scan_env_marker_refs

from tai42_skeleton.marketplace.provides import env_selected_items, mcp_entry_items


def install_notes(spec: PluginSpec) -> list[str]:
    """Activation notes: one per env-selected item (installed but inactive until
    ``TAI_CONFIG_MODE`` selects it — ``file`` is built in, every other mode resolves
    by convention to the installed provider's module), plus one per mcp-server item
    naming the mounted server title."""
    notes = [
        f"{item.name!r} is installed but inactive: TAI_CONFIG_MODE selects the config provider — "
        "'file' is built in, every other mode resolves by convention to the "
        "'tai42_config_<mode>.manager' the installed provider ships"
        for item in env_selected_items(spec)
    ]
    notes.extend(
        f"mounted MCP server {item.name!r} in the manifest 'mcp' section; its tools go live on the reload"
        for item in mcp_entry_items(spec)
    )
    return notes


def uninstall_notes(spec: PluginSpec) -> list[str]:
    """Removal notes: one per env-selected item (if ``TAI_CONFIG_MODE`` currently
    selects the removed provider, the next boot fails importing it until re-pointed
    or unset), plus one per mcp-server item — its manifest entry is removed but the
    stored env values its ``!ENV`` markers referenced are LEFT (never silently
    delete operator secrets); the note names those orphaned vars, scanned from the
    entry BEFORE removal."""
    notes = [
        f"{item.name!r} was a config provider: if TAI_CONFIG_MODE currently selects it, the next boot will "
        "fail importing the removed provider until you re-point or unset TAI_CONFIG_MODE"
        for item in env_selected_items(spec)
    ]
    for item in mcp_entry_items(spec):
        assert item.mcp is not None  # guaranteed for the mcp-server kind
        orphaned = sorted({ref.var for ref in scan_env_marker_refs(item.mcp.model_dump(exclude_none=True))})
        if orphaned:
            notes.append(
                f"removed MCP server {item.name!r}; its stored env value(s) are LEFT untouched — "
                f"{', '.join(orphaned)} may now be orphaned (remove them by hand if no other entry uses them)"
            )
        else:
            notes.append(f"removed MCP server {item.name!r} from the manifest 'mcp' section")
    return notes
