"""The post-record remount reload over a booted app whose config store other doors write."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.config.service import ApplyResult, ConfigService
from tai42_skeleton.marketplace import manifest_apply
from tai42_skeleton.operations import tool_extensions

from .._fakes.combo_check_app import LiveStore, booted_check_app, clean_server, live_store

# The bound on a write whose last step is a full config reload. A reload is CPU-bound (it
# re-imports the manifest modules, rebinds every operation tool and reads the installed
# distributions' metadata) and hands work between the serving loop and worker threads, so a
# loaded host stretches it twenty times over and more; the bound only stops a hung reload
# from holding the run.
_RELOAD_BOUND_S = 60.0


async def _reached(event: asyncio.Event, what: str) -> None:
    """Wait for ``event``; fail the test when it is not set within 10 s."""
    try:
        await asyncio.wait_for(event.wait(), 10)
    except TimeoutError:
        pytest.fail(f"{what} was never reached")


@pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)
def test_a_remount_reload_keeps_a_write_that_landed_while_it_waited(
    live_store: LiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    combo = {"name": "refusing", "config": {"label": "ok"}}
    holding, release = asyncio.Event(), asyncio.Event()
    refuse_held_unpicks = ConfigService._refuse_held_unpicks

    async def held(self: ConfigService, *args: Any, **kwargs: Any) -> None:
        if not release.is_set():
            holding.set()
            await _reached(release, "the remount's start")
        await refuse_held_unpicks(self, *args, **kwargs)

    monkeypatch.setattr(ConfigService, "_refuse_held_unpicks", held)
    prior = ApplyResult(fleet=FleetResult(op="reload_config", results=[]), local={"status": "ok"})

    async def run() -> None:
        async with booted_check_app():
            edit = asyncio.create_task(tool_extensions.modify_tool_extension_combos("echo", add=[[combo]]))
            await _reached(holding, "the combo edit's write exclusion")
            svc = ConfigService.from_app()
            remount = asyncio.create_task(manifest_apply.remount_reload(svc, prior))
            for _ in range(50):
                await asyncio.sleep(0)
            release.set()
            answer = await asyncio.wait_for(edit, _RELOAD_BOUND_S)
            result = await asyncio.wait_for(remount, _RELOAD_BOUND_S)
            assert answer["combos"] == [[combo]]
            assert result is not prior
            assert live_store.manifest["tools"][0]["extensions"] == {"echo": [[combo]]}

    asyncio.run(run())
