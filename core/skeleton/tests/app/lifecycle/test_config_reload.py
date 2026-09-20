"""Soft config reload: env refresh into the process and dropping keys removed at source."""

from __future__ import annotations

import asyncio

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest


def test_reload_config_refreshes_env_and_reinitializes(monkeypatch):
    import os

    from tai42_kit.clients.base import current_client_epoch

    from tai42_skeleton.app.reload_gate import reload_gate

    captured_env = {"NEW_KEY": "v1", "OTHER": "v2"}

    async def run():
        async with app.app_context(Manifest.model_validate({})):
            monkeypatch.setattr(app.config.config_manager, "read_env", lambda: captured_env)
            monkeypatch.setattr(app.config.config_manager, "read_manifest", dict)

            before = current_client_epoch()
            # Driven off the serving loop, as production does (the reload-gate worker
            # thread): a fresh epoch is built under the refreshed env and swapped in.
            out = await reload_gate.run(app.admin.reload_config, reimports=True)
            assert out == {"status": "ok", "env_keys": 2}
            # Env refreshed into the process environment.
            assert os.environ["NEW_KEY"] == "v1"
            assert os.environ["OTHER"] == "v2"
            # Reinitialized: the client epoch advanced (a fresh serving epoch swapped in).
            assert current_client_epoch() == before + 1

    try:
        asyncio.run(run())
    finally:
        os.environ.pop("NEW_KEY", None)
        os.environ.pop("OTHER", None)


def test_reload_config_drops_env_keys_removed_from_source(monkeypatch):
    import os

    from tai42_skeleton.app.reload_gate import reload_gate

    async def run():
        async with app.app_context(Manifest.model_validate({})):
            monkeypatch.setattr(app.config.config_manager, "read_manifest", dict)

            monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {"K1_RC": "a", "K2_RC": "b"})
            await reload_gate.run(app.admin.reload_config, reimports=True)
            assert os.environ["K1_RC"] == "a"
            assert os.environ["K2_RC"] == "b"

            # K2 removed from the source env: the next reload must drop it, not
            # leave it lingering as stale config.
            monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {"K1_RC": "a"})
            await reload_gate.run(app.admin.reload_config, reimports=True)
            assert os.environ["K1_RC"] == "a"
            assert "K2_RC" not in os.environ

    try:
        asyncio.run(run())
    finally:
        os.environ.pop("K1_RC", None)
        os.environ.pop("K2_RC", None)
