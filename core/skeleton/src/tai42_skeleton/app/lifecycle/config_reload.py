"""The soft config reload — build and swap a fresh serving epoch under the persisted env."""

import asyncio
from typing import Any

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.lifecycle.state import LifecycleState


class ConfigReloadMixin(LifecycleState):
    def _reload_config(self) -> dict[str, Any]:
        """Soft restart: build a FRESH serving epoch under the persisted env and swap
        it in atomically — env refresh + settings reset + registry rebuild +
        fresh serving surface + retire of the previous generation, all in the one
        ``build_and_swap_epoch`` primitive. Heavy but in-process — no pod restart. A
        reload-added router serves after the swap (the epoch's fresh FastMCP snapshots
        the new route table); a failed build keeps the old epoch serving untouched.

        The build's fresh FastMCP lifespan is loop-affine, so the swap runs ON the
        serving loop even though this body is driven from a reload-gate worker thread.
        """
        env = self._config_manager.read_env()

        from tai42_skeleton.app.epoch import _reload_driven_by_request, build_and_swap_epoch

        # Read in THIS context (the reload-gate worker thread, into which
        # ``asyncio.to_thread`` copied the driving request's context): true when a
        # door request drove this reload, so the retire excuses that still-admitted
        # request instead of self-waiting the full drain budget on it. Captured here
        # and passed explicitly — ``_swap`` runs on the serving loop via
        # ``run_coroutine_threadsafe`` and does not inherit this thread's context.
        driven_by_request = _reload_driven_by_request.get()

        async def _swap() -> dict[str, Any]:
            # Release the loop-bound langgraph checkpoint/store pools before the
            # build's settings reset drops their per-loop registries — it refuses to
            # drop a registry still holding live resources on a running loop.
            await self._close_llm_registries()
            await build_and_swap_epoch(env, drain_tolerate_driver=driven_by_request)
            return {"status": "ok", "env_keys": len(env)}

        loop = self._serving_loop
        if loop is not None and loop.is_running():
            try:
                running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                raise RuntimeError("reload_config must be driven off the serving loop (via reload_gate.run)")
            # Marshal the swap onto the serving loop and block-wait its result.
            return asyncio.run_coroutine_threadsafe(_swap(), loop).result()
        # No serving loop bound (a pure-sync context / test): drive the swap on a
        # throwaway loop of this caller's own.
        return asyncio.run(_swap())

    async def _close_llm_registries(self) -> None:
        """Close the langgraph checkpoint + store resource pools — the release a
        settings reset requires before it can drop the per-loop registries."""
        await _lifecycle.checkpoint_registry().close_all()
        await _lifecycle.store_registry().close_all()
