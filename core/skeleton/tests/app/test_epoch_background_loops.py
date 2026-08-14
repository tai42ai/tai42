"""The loop-affine background loops survive a reload: the marketplace advisories poll and
the conversations delivery sweep must still be running on the LIVE serving loop after an
epoch swap, not left dead on the throwaway build-thread loop the per-epoch handlers run on.

These drive ``reload_config`` (which collapses onto ``build_and_swap_epoch``) through the
reload gate exactly as production does. The two loops are (re)established by the post-swap
hook the primitive runs ON the serving loop after each swap, registered with the new epoch
so they retire with their generation. Test-local ``on_post_swap`` establishers call the
REAL ``advisories.start_poll`` / ``delivery.start_delivery_sweep`` so the actual poll and
sweep tasks are asserted — bypassing only the routers' store-configured guards, which are
not the mechanism under test. The loops sleep on their (long) intervals, so neither touches
a store during the test.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace import advisories as advisories_module


@pytest.fixture(autouse=True)
def _restore_process_env():
    """A successful build+swap leaves its applied env live in ``os.environ``. Snapshot and
    restore it around each test, and reset the epoch's loaded-key set + the settings cache."""
    from tai42_skeleton.app import epoch as epoch_mod

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = set()
    reset_all_settings()


def _patch_reload(monkeypatch, *, manifest: dict, env: dict[str, str]) -> None:
    monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: manifest)
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: env)


def test_advisories_poll_and_delivery_sweep_survive_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    # The poll is enabled with a long interval so its task just sleeps; the sweep's default
    # interval is likewise well beyond the test, so neither loop reaches a store pass.
    monkeypatch.setenv("MARKETPLACE_ADVISORIES_POLL", "true")
    monkeypatch.setenv("MARKETPLACE_ADVISORIES_INTERVAL_S", "3600")
    reset_all_settings()

    def _establish_poll() -> None:
        advisories_module.start_poll()

    def _establish_sweep() -> None:
        delivery_module.start_delivery_sweep()

    # Register the establishers on the post-swap hook so they run on the serving loop at
    # boot and after every swap — the exact seam the real router handlers use.
    app.lifecycle.on_post_swap(_establish_poll)
    app.lifecycle.on_post_swap(_establish_sweep)
    keys = [
        f"{_establish_poll.__module__}.{_establish_poll.__qualname__}",
        f"{_establish_sweep.__module__}.{_establish_sweep.__qualname__}",
    ]

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "none"})):
            serving_loop = asyncio.get_running_loop()

            # Boot established both loops on the serving loop.
            poll_boot = advisories_module._poll_task
            sweep_boot = delivery_module._sweep_task
            assert poll_boot is not None
            assert not poll_boot.done()
            assert sweep_boot is not None
            assert not sweep_boot.done()
            assert poll_boot.get_loop() is serving_loop
            assert sweep_boot.get_loop() is serving_loop

            # Drive a REAL reload through the gate (collapses onto build_and_swap_epoch).
            _patch_reload(monkeypatch, manifest={"default_routers": "none"}, env={"ACCESS_CONTROL_ENABLE": "false"})
            await reload_gate.run(app.admin.reload_config, reimports=True)

            # Both loops are STILL running after the reload — re-established as fresh tasks
            # on the SAME live (not-closed) serving loop, not left dead on the build loop.
            poll_after = advisories_module._poll_task
            sweep_after = delivery_module._sweep_task
            assert poll_after is not None
            assert not poll_after.done()
            assert sweep_after is not None
            assert not sweep_after.done()
            assert not serving_loop.is_closed()
            assert poll_after.get_loop() is serving_loop
            assert sweep_after.get_loop() is serving_loop

            # The boot generation's loops were retired (cancelled) — no timer outlives its
            # epoch, and the fresh tasks are genuinely new.
            assert poll_after is not poll_boot
            assert sweep_after is not sweep_boot
            assert poll_boot.cancelled() or poll_boot.done()
            assert sweep_boot.cancelled() or sweep_boot.done()

            # Stop the live loops before leaving the context (this manifest mounts no
            # router whose on_shutdown would).
            await advisories_module.stop_poll()
            await delivery_module.stop_delivery_sweep()

    try:
        asyncio.run(run())
    finally:
        for key in keys:
            app._post_swap_handlers.pop(key, None)
        advisories_module._poll_task = None
        delivery_module._sweep_task = None
        reset_all_settings()
