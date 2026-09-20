"""Test helper: drive a reload for a GIVEN manifest through the real build-aside +
swap path.

Production reads the manifest from the config manager (``_default_rebuild``); a unit
reload test wants to hand a specific in-memory manifest instead. This drives the same
``build_and_swap_epoch`` primitive with a rebuild that installs the given manifest —
so the build-aside-on-failure and swap-on-success contracts are exercised —
but with a sentinel serving app in place of a real ``http_app`` (the unit reload tests
assert the registry surface, not the ASGI dispatch).
"""

from __future__ import annotations

from typing import Any

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.epoch import Epoch


class _SentinelServingApp:
    async def __call__(self, scope, receive, send) -> None:  # pragma: no cover
        raise AssertionError("the unit reload helper's sentinel serving app is never dispatched")


async def reload_with(app: Any, manifest: Any) -> Epoch:
    """Rebuild a fresh epoch under ``manifest`` and swap it in, or discard it and
    re-raise on a failed build (the live epoch keeps serving untouched)."""

    def _rebuild() -> None:
        app._building = app._build_serving_core()
        try:
            app.lifecycle.reload_registries(manifest)
        except BaseException:
            app._building = None
            raise

    async def _build_serving_app(epoch: Epoch):
        core = app._building
        try:
            epoch.core = core
        finally:
            app._building = None
        return _SentinelServingApp()

    return await epoch_mod.build_and_swap_epoch({}, rebuild=_rebuild, build_serving_app=_build_serving_app)
