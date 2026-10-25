"""Extension settings are declared by a JSON Schema and checked before any manifest write.

``GET /api/extensions`` (and the projected MCP ``list_extensions``) serves each extension's
``config_schema``: ``output_schema`` and ``ask_external`` declare one, ``cache`` takes no settings.
A write whose settings the dry-run bind refuses answers 400 with the bind's own text and leaves the
stored manifest and the served tools as they were: a bare ``output_schema`` combo, a schema whose
``$ref`` does not resolve, settings refused on a base no server serves yet, and an ``!ENV`` marker
resolving to a value of the wrong type (whose value never reaches the reply).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp.client.client import CallToolResult

from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "probe-tools", "mutable", "setting:extension:output_schema", "setting:manifest-bound-extension-config"
    ),
]

_TOOL = "e2e_worker_info"
_EXTENSIONS_PATH = f"/api/tools/{_TOOL}/extensions"
_META_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_PREFIX = f"invalid extensions for tool '{_TOOL}': tools[e2e-probes].extensions[{_TOOL}][0]: "
_UNRESOLVED_REF_COMBOS = [[{"name": "output_schema", "config": {"schema": {"properties": {"a": {"$ref": "#/nope"}}}}}]]
_UNRESOLVED_REF_ERROR = (
    f"{_PREFIX}extension 'output_schema' on tool '{_TOOL}' cannot bind: JSON schema property 'a' breaks the JSON "
    "Schema rules: $ref '#/nope' does not resolve: nothing at '/nope'"
)
_SECRET = "s3cr3t-e2e-value"
_MARKER_COMBOS = [[{"name": "output_schema", "config": {"schema": "!ENV ${E2E_SETTING_SECRET}"}}]]
_MARKER_ERROR = (
    f"{_PREFIX}extension 'output_schema' on tool '{_TOOL}' cannot bind: extension 'output_schema' config: value does "
    "not match schema at $.schema: breaks the schema's 'type' rule: 'object', 'boolean' (the value is not shown)"
)
_LATE_ENTRY = {
    "title": "late",
    "config": {"url": "http://127.0.0.1:1/mcp"},
    "extensions": {"fetch": [[{"name": "output_schema", "config": {}}]]},
}
_LATE_ERROR = (
    "invalid mcp config: mcp[late].extensions[fetch][0]: extension 'output_schema' on tool 'fetch' cannot bind: "
    "extension 'output_schema' config: value does not match schema at $: breaks the schema's 'required' rule: "
    "'schema' (the value is not shown)"
)


def _error_text(result: CallToolResult) -> str:
    return "".join(getattr(block, "text", "") for block in (result.content or []))


async def _preserved(stack: TaiStack) -> dict[str, Any]:
    return await stack.api().get("/api/manifest/preserved", retry_on_reloading=True)


async def _served_tools(stack: TaiStack) -> list[str]:
    async with stack.mcp() as mcp:
        return sorted(await mcp.tool_names())


async def test_the_catalog_serves_each_extension_config_schema(extensions_stack: TaiStack) -> None:
    listing = await extensions_stack.api().get("/api/extensions", retry_on_reloading=True)
    by_name = {entry["name"]: entry for entry in listing}
    output_schema = by_name["output_schema"]["config_schema"]
    assert output_schema["properties"]["schema"]["$ref"] == _META_SCHEMA
    assert output_schema["required"] == ["schema"]
    assert by_name["ask_external"]["config_schema"]["type"] == "object"
    assert by_name["cache"]["config_schema"] is None

    async with extensions_stack.mcp() as mcp:
        result = await mcp.call_tool("list_extensions", {}, retry_on_reloading=True)
    assert isinstance(result.structured_content, dict), result
    assert result.structured_content["result"] == listing


async def test_a_bare_output_schema_combo_is_refused_naming_schema(extensions_stack: TaiStack) -> None:
    before = await _preserved(extensions_stack)
    refused = await extensions_stack.api().post(
        _EXTENSIONS_PATH, json={"combos": [["output_schema"]]}, expect=400, retry_on_reloading=True
    )
    assert "'schema'" in refused["error"], refused
    assert await _preserved(extensions_stack) == before


async def test_a_schema_with_an_unresolved_ref_is_refused_on_every_door(extensions_stack: TaiStack) -> None:
    before = await _preserved(extensions_stack)
    tools_before = await _served_tools(extensions_stack)

    refused = await extensions_stack.api().post(
        _EXTENSIONS_PATH, json={"combos": _UNRESOLVED_REF_COMBOS}, expect=400, retry_on_reloading=True
    )
    assert refused == {"error": _UNRESOLVED_REF_ERROR}

    async with extensions_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "set_tool_extensions",
            {"name": _TOOL, "combos": _UNRESOLVED_REF_COMBOS},
            raise_on_error=False,
            retry_on_reloading=True,
        )
    assert result.is_error, result
    assert _error_text(result) == _UNRESOLVED_REF_ERROR

    assert await _preserved(extensions_stack) == before
    assert await _served_tools(extensions_stack) == tools_before


async def test_settings_on_a_base_no_server_serves_yet_are_refused(extensions_stack: TaiStack) -> None:
    refused = await extensions_stack.api().post(
        "/api/mcp-config/entries", json={"entries": [_LATE_ENTRY]}, expect=400, retry_on_reloading=True
    )
    assert refused == {"error": _LATE_ERROR}
    stored = await _preserved(extensions_stack)
    assert all(entry["title"] != "late" for entry in stored["mcp"]), stored["mcp"]


async def test_a_marker_resolving_to_a_wrong_type_is_refused_without_its_value(extensions_stack: TaiStack) -> None:
    api = extensions_stack.api()
    await api.post("/api/config/env", json={"env": {"E2E_SETTING_SECRET": _SECRET}}, retry_on_reloading=True)
    before = await _preserved(extensions_stack)

    refused = await api.post(_EXTENSIONS_PATH, json={"combos": _MARKER_COMBOS}, expect=400, retry_on_reloading=True)
    assert refused == {"error": _MARKER_ERROR}
    raw = await api.request_raw("POST", _EXTENSIONS_PATH, json={"combos": _MARKER_COMBOS})
    assert raw.status_code == 400, raw.text
    assert _SECRET not in raw.text

    async with extensions_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "set_tool_extensions",
            {"name": _TOOL, "combos": _MARKER_COMBOS},
            raise_on_error=False,
            retry_on_reloading=True,
        )
    assert result.is_error, result
    assert _error_text(result) == _MARKER_ERROR
    assert _SECRET not in str(result)

    assert await _preserved(extensions_stack) == before
