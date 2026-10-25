"""The manifest reads serve the extension settings the stack was booted with.

``GET /api/manifest`` and ``GET /api/manifest/preserved`` carry ``extension_settings``: the
loaded ``extensions_modules`` and every ``tools`` entry's per-tool combos. The probe tools entry
binds ``e2e_record`` to ``[["cache"]]``. The projected MCP ``get_manifest`` tool answers the same
``extension_settings`` as the HTTP read.
"""

from __future__ import annotations

from typing import Any

import pytest

from tai42_e2e.manifests.tool_entries import PROBE_TOOLS_TITLE
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "probe-tools", "setting:extension:cache", "setting:extension:monitor", "setting:pristine-combo-state"
    ),
]

_EXTENSIONS_MODULES = [
    "tai42_toolbox.extensions.batch",
    "tai42_toolbox.extensions.cache",
    "tai42_toolbox.extensions.chain",
    "tai42_toolbox.extensions.output_schema",
    "tai42_skeleton.extensions.builtin.monitor",
    "tai42_skeleton.extensions.builtin.ask_external",
]


def _probe_entry(settings: dict[str, Any]) -> dict[str, Any]:
    entries = [entry for entry in settings["tools"] if entry["title"] == PROBE_TOOLS_TITLE]
    assert len(entries) == 1, f"the probe tools entry is not served exactly once: {settings['tools']}"
    return entries[0]


@pytest.mark.parametrize("path", ["/api/manifest", "/api/manifest/preserved"])
async def test_the_manifest_reads_serve_the_extension_settings(extensions_stack: TaiStack, path: str) -> None:
    body = await extensions_stack.api().get(path, retry_on_reloading=True)
    settings = body["extension_settings"]
    assert settings["extensions_modules"] == _EXTENSIONS_MODULES
    assert _probe_entry(settings)["extensions"]["e2e_record"] == [["cache"]]


async def test_the_projected_get_manifest_tool_serves_the_same_extension_settings(extensions_stack: TaiStack) -> None:
    served = (await extensions_stack.api().get("/api/manifest", retry_on_reloading=True))["extension_settings"]
    async with extensions_stack.mcp() as mcp:
        result = await mcp.call_tool("get_manifest", {}, retry_on_reloading=True)
    assert isinstance(result.structured_content, dict), result
    assert result.structured_content["extension_settings"] == served
