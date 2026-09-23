"""Every preset read surface returns the stored ``!ENV`` reference verbatim, never a
resolved value.

A ``fixed_kwargs`` scalar leaf written ``!ENV ${VAR}`` is a secret reference: the
store keeps the marker, ``preset_bind`` resolves it against ``os.environ`` at bind for
the in-memory tool only. These oracles set the referenced variable in the environment
(so a leak WOULD surface a real value) and assert every read door — HTTP
``get_preset`` / ``get_version`` / ``list_versions`` and the same operations projected
as MCP tools — returns the marker string unchanged, while the record-only
``list_presets`` view carries no ``fixed_kwargs`` body at all and so cannot ride the
value out either.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tai42_skeleton.app import instance
from tai42_skeleton.operations import presets as preset_ops

from .conftest import _create, _manifest

_VAR = "PRESET_READ_SECRET_VAR"
_RESOLVED = "resolved-credential-value"
_MARKER = f"!ENV ${{{_VAR}}}"


@pytest.fixture(autouse=True)
def _referenced_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The referenced variable is PRESENT, so a resolving read would surface the real
    # value — the reads under test must still return only the marker.
    monkeypatch.setenv(_VAR, _RESOLVED)


def test_http_reads_return_the_reference_verbatim(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("ref", base_tool="echo", fixed_kwargs={"text": _MARKER})

            # get one: the active body carries the marker, not the resolved value.
            detail = await preset_ops.get_preset(name="ref")
            assert detail["fixed_kwargs"] == {"text": _MARKER}

            # get a version + list versions: the immutable version body carries the marker.
            version = await preset_ops.get_version(name="ref", version="1")
            assert version["body"]["fixed_kwargs"] == {"text": _MARKER}
            versions = await preset_ops.list_versions(name="ref")
            assert [v["body"]["fixed_kwargs"] for v in versions] == [{"text": _MARKER}]

            # The resolved value never appears on any read surface.
            assert _RESOLVED not in repr(detail)
            assert _RESOLVED not in repr(versions)

    asyncio.run(run())


def test_list_view_carries_no_kwargs_body(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("ref", base_tool="echo", fixed_kwargs={"text": _MARKER})

            row = next(r for r in await preset_ops.list_presets() if r["name"] == "ref")
            # The list view is record metadata only — it never carries the baked
            # ``fixed_kwargs`` body, so neither the marker nor a resolved value rides it.
            assert "fixed_kwargs" not in row
            assert _RESOLVED not in repr(row)

    asyncio.run(run())


class _CapturingTools:
    """Captures the tool functions ``project_operations`` registers, keyed by name."""

    def __init__(self) -> None:
        self.captured: dict[str, Callable[..., Awaitable[Any]]] = {}

    def tool(self, *, force, name, tags, annotations):
        def register(fn):
            self.captured[name] = fn
            return fn

        return register


class _CapturingApp:
    def __init__(self) -> None:
        self.tools = _CapturingTools()


def test_mcp_projected_reads_return_the_reference_verbatim(pg) -> None:
    async def run() -> None:
        from tai42_contract.manifest import ApiToolsConfig

        from tai42_skeleton.operations import operation_registry, project_operations

        async with instance.app.app_context(_manifest()):
            await _create("ref", base_tool="echo", fixed_kwargs={"text": _MARKER})

            # The MCP surface projects each read operation as a tool wrapping the SAME
            # function the HTTP door serves; project it and dispatch it directly.
            projected = _CapturingApp()
            project_operations(projected, ApiToolsConfig(enabled=True), registry=operation_registry)

            detail = await projected.tools.captured["get_preset"](name="ref")
            assert detail["fixed_kwargs"] == {"text": _MARKER}

            version = await projected.tools.captured["get_version"](name="ref", version="1")
            assert version["body"]["fixed_kwargs"] == {"text": _MARKER}

            assert _RESOLVED not in repr(detail)
            assert _RESOLVED not in repr(version)

    asyncio.run(run())
