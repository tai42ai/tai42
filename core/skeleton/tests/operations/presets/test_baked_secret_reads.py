"""Every preset read door masks a literal credential baked under a secret-typed kwarg,
while the write/execute paths keep the real value.

``secret_sink`` has a ``SecretStr`` ``token`` (rendered ``format: password`` /
``writeOnly``) and a plain ``label``. A preset bakes a literal into ``token``; each
read door — HTTP ``get_preset`` / ``get_version`` / ``list_versions`` and the same
operations projected as MCP tools — returns the mask for ``token`` and the authored
value for ``label``. With the base tool unresolvable the read fails closed and masks
every baked leaf.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from tai42_contract.secrets import SECRET_PLACEHOLDER

from tai42_skeleton.app import instance
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.tools.binding.errors import UnknownToolError

from .conftest import _create, _manifest

_SECRET = "s3kr3t-literal"
_BAKED = {"token": _SECRET, "label": "public"}


def test_http_reads_mask_the_baked_secret(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("vaulted", base_tool="secret_sink", fixed_kwargs=_BAKED)

            detail = await preset_ops.get_preset(name="vaulted")
            assert detail["fixed_kwargs"] == {"token": SECRET_PLACEHOLDER, "label": "public"}

            version = await preset_ops.get_version(name="vaulted", version="1")
            assert version["body"]["fixed_kwargs"] == {"token": SECRET_PLACEHOLDER, "label": "public"}

            versions = await preset_ops.list_versions(name="vaulted")
            assert [v["body"]["fixed_kwargs"] for v in versions] == [{"token": SECRET_PLACEHOLDER, "label": "public"}]

            # The literal never rides any read surface.
            assert _SECRET not in repr(detail)
            assert _SECRET not in repr(versions)

    asyncio.run(run())


def test_stored_body_keeps_the_real_value(pg) -> None:
    # The redaction is a read-view: the store still holds the literal, so a bind /
    # execute still sees it. Only the read doors mask.
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("vaulted", base_tool="secret_sink", fixed_kwargs=_BAKED)
            body = (await instance.app.presets.list_active_bodies())["vaulted"]
            assert body.fixed_kwargs == _BAKED

    asyncio.run(run())


class _CapturingTools:
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


def test_mcp_projected_reads_mask_the_baked_secret(pg) -> None:
    async def run() -> None:
        from tai42_contract.manifest import ApiToolsConfig

        from tai42_skeleton.operations import operation_registry, project_operations

        async with instance.app.app_context(_manifest()):
            await _create("vaulted", base_tool="secret_sink", fixed_kwargs=_BAKED)

            projected = _CapturingApp()
            project_operations(projected, ApiToolsConfig(enabled=True), registry=operation_registry)

            detail = await projected.tools.captured["get_preset"](name="vaulted")
            assert detail["fixed_kwargs"] == {"token": SECRET_PLACEHOLDER, "label": "public"}

            version = await projected.tools.captured["get_version"](name="vaulted", version="1")
            assert version["body"]["fixed_kwargs"] == {"token": SECRET_PLACEHOLDER, "label": "public"}

            assert _SECRET not in repr(detail)
            assert _SECRET not in repr(version)

    asyncio.run(run())


def test_read_fails_closed_when_the_base_tool_is_unresolvable(pg, monkeypatch, caplog) -> None:
    # An unbindable preset (its base tool's plugin absent) offers no schema to tell a
    # secret leaf from a config one, so a read masks every baked leaf rather than leak,
    # and logs a warning naming the preset + base tool so the degraded view is visible.
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("vaulted", base_tool="secret_sink", fixed_kwargs=_BAKED)

            async def _raise(_key: str):
                raise UnknownToolError("secret_sink")

            monkeypatch.setattr(instance.app.tools, "get_tool", _raise)
            with caplog.at_level(logging.WARNING, logger="tai42_skeleton.operations.presets.read"):
                detail = await preset_ops.get_preset(name="vaulted")
            assert detail["fixed_kwargs"] == {"token": SECRET_PLACEHOLDER, "label": SECRET_PLACEHOLDER}
            warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
            assert "vaulted" in warning.getMessage()
            assert "secret_sink" in warning.getMessage()

    asyncio.run(run())
