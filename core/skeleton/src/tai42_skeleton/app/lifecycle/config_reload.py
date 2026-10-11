"""The soft config reload — build and swap a fresh serving epoch under the persisted env."""

import asyncio
from typing import Any

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.lifecycle.state import LifecycleState


def awaiting_respawn() -> list[str]:
    """The recycle-class env keys whose effective value differs from the value this process bound them under.

    A key is bound at boot, or when the plugin that declares it was loaded; a pooled resource of
    this process was built under the bound value, so the key takes effect only when the process is
    respawned. Classified by the live settings registry. Names only, sorted.
    """
    from tai42_kit.settings import respawn_class_env_drift

    from tai42_skeleton.config.boundary import reload_class_by_env_var

    classes = reload_class_by_env_var()
    return sorted(key for key in respawn_class_env_drift() if classes.get(key) == "recycle")


class ConfigReloadMixin(LifecycleState):
    """The soft config-reload lifecycle step: build and swap a fresh serving epoch in place."""

    def _reload_config(self) -> dict[str, Any]:
        """Soft restart: build a FRESH serving epoch under the persisted env and swap it in atomically.

        Env refresh + settings reset + registry rebuild + fresh serving surface + retire of the
        previous generation, all in the one ``build_and_swap_epoch`` primitive. Heavy but
        in-process — no pod restart. A reload-added router serves after the swap (the epoch's
        fresh FastMCP snapshots the new route table); a failed build keeps the old epoch serving
        untouched.

        The build's fresh FastMCP lifespan is loop-affine, so the swap runs ON the
        serving loop even though this body is driven from a reload-gate worker thread,
        and blocks that loop for the rebuild's length: health and readiness probes, MCP
        keepalives, in-flight streams and the generation's timers pause until the swap
        returns. The worker-bus heartbeat runs on its own thread and does not pause.

        The result ``{"status", "env_keys", "awaiting_respawn"}`` states, as a fact, the
        recycle-class keys whose value now differs from the one this process bound them under —
        at boot, or when the plugin that declares them was loaded (:func:`awaiting_respawn`): the
        soft restart carries every hot key in place, and a pooled resource of this process still
        runs on the bound value of those keys. The operation that started the reload judges the
        fact — it alone knows whether a respawn follows.
        """
        env = self._config_manager.read_env()

        from tai42_skeleton.app.epoch import build_and_swap_epoch

        async def _swap() -> dict[str, Any]:
            # Release the loop-bound kit resources before the build's settings reset
            # drops their per-loop registries — it refuses to drop a registry still
            # holding live resources on a running loop.
            await _lifecycle.release_loop_bound_resources()
            await build_and_swap_epoch(env)
            return {"status": "ok", "env_keys": len(env), "awaiting_respawn": awaiting_respawn()}

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
