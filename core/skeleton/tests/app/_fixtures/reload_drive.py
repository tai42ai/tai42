"""Fixture tool module: a tool that drives the real config reload, and a call held until released."""

from __future__ import annotations

import time

from tai42_contract.app import tai42_app
from tests.app import _reload_drive_state


@tai42_app.tools.tool
async def drive_reload() -> dict:
    """Run the real config reload through the reload gate and report what the reload saw."""
    from tai42_skeleton.app import epoch as epoch_mod
    from tai42_skeleton.app.reload_gate import reload_gate

    state = _reload_drive_state.STATE
    old = epoch_mod.current_epoch()
    seen: dict[str, object] = {}

    def _body() -> object:
        seen["engaged"] = old.engaged
        return tai42_app.admin.reload_config()

    start = time.monotonic()
    await reload_gate.run(_body, reimports=False)
    observed = {"engaged": seen["engaged"], "elapsed": time.monotonic() - start}
    state.drives.append(observed)
    state.driver_swapped.set()
    if state.hold_driver:
        await state.release_driver.wait()
    return observed


@tai42_app.tools.tool
async def held_call() -> str:
    """Answer only once the test releases it."""
    state = _reload_drive_state.STATE
    state.held_started.set()
    await state.release_held.wait()
    return "released"
