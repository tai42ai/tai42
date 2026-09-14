"""The live-manifest MCP re-read graft."""

from __future__ import annotations

import asyncio

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

from ._doubles import _cfg


def test_refresh_manifest_mcp_grafts_reread_rows(monkeypatch):
    async def run():
        async with app.app_context(Manifest.model_validate({})):
            fresh = Manifest.model_validate({"mcp": [_cfg("late").model_dump()]})
            monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: fresh.model_dump())
            app._refresh_manifest_mcp()
            assert app._manifest is not None
            assert "late" in (app._manifest.mcp_map or {})

    asyncio.run(run())
