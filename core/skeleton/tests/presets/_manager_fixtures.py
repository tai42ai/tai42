"""Shared helpers for the ``PresetManager`` register/reload test groups: the base
manifest, live-tool-name inspection, and the create-and-register two-step."""

from __future__ import annotations

from fastmcp.tools.base import Tool
from tai42_contract.agent.base import PresetSpec

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

from ..versioning.conftest import FakeVersioningPg

__all__ = ["FakeVersioningPg", "_create_versioned", "_live_tool_names", "_manifest"]

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "echo"]}],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


def _live_tool_names() -> list[str]:
    """Every tool name held by the live FastMCP provider, WITH duplicates — so a
    reload that leaked a second copy of a branch shows up as a repeated name (a
    plain ``get_tools()`` dict would collapse it)."""
    components = app.fastmcp.local_provider._components
    return [c.name for c in components.values() if isinstance(c, Tool)]


async def _create_versioned(name: str, base_tool: str, fixed_kwargs, extensions, description="d") -> None:
    """Persist a versioned preset AND register it — the create route's two steps."""
    await app.presets.store.create_preset(
        PresetSpec(name=name, description=description, base_tool=base_tool, fixed_kwargs=fixed_kwargs),
        extensions=extensions,
    )
    body = await app.presets.store.get_active_body(name)
    await app.preset_manager.register(name, body.base_tool, body.fixed_kwargs, body.extensions, body.description)
