"""A discovered provider descriptor is accepted by every contract model that carries a descriptor."""

from __future__ import annotations

from typing import Any

from tai42_contract.manifest import Manifest
from tai42_contract.plugins import PluginItem, PluginItemKind

_DISCOVERED: dict[str, Any] = {
    "id": "remote",
    "display_name": "Remote",
    "kind": "oauth",
    "origin": "community",
    "category": "dev",
    "sub_services": {
        "files": {
            "id": "files",
            "display_name": "Files",
            "mcp_server": {"type": "http", "url": "https://mcp.example.com/mcp"},
        }
    },
}


def test_the_manifest_accepts_a_discovered_connector():
    manifest = Manifest.model_validate({"connectors": [_DISCOVERED]})
    assert manifest.connectors[0].is_discovered
    dumped = manifest.model_dump(mode="json")
    assert dumped["connectors"][0]["oauth"] is None
    assert dumped["connectors"][0]["icon_url"] is None


def test_a_plugin_item_carries_a_discovered_connector():
    item = PluginItem.model_validate(
        {"kind": PluginItemKind.CONNECTOR, "name": "remote", "provider": _DISCOVERED, "description": "A connector."}
    )
    assert item.provider is not None
    assert item.provider.is_discovered
