"""The lifecycle handler registries and the runners that fire them."""

import inspect
import logging
from collections.abc import Callable
from typing import Any

from tai42_skeleton.app.lifecycle.state import LifecycleState

logger = logging.getLogger(__name__)


class LifecycleHandlersMixin(LifecycleState):
    def _on_startup(self, func: Callable):
        self._startup_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    def _on_reload(self, func: Callable):
        """Register a handler to re-run after every in-place re-init."""
        self._reload_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    def _on_post_swap(self, func: Callable):
        """Register a handler that establishes a loop-affine background loop on the
        serving loop — run at boot and after every epoch swap, never on the throwaway
        build-thread loop the per-epoch handlers run on."""
        self._post_swap_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    def _on_fleet_op_applied(self, func: Callable):
        """Register a handler fired with the OP NAME after every applied bus op and
        after the reconnect self-resync reload. Keyed by qualified name so a module
        re-import replaces rather than accumulates."""
        self._fleet_op_applied_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    def _tool_reloader(self, kind: str) -> Callable:
        """Register an ``(action, name) -> dict`` reloader for one tool kind.
        Last registration wins (module re-imports re-run the decorator)."""

        def decorator(func: Callable) -> Callable:
            self._tool_reloaders[kind] = func
            return func

        return decorator

    async def _run_tool_reload(self, kind: str, action: str, name: str) -> dict[str, Any]:
        """Apply one tool reload/remove via the registered reloader. Raises on
        an unknown kind/action or a failing reloader."""
        if action not in ("reload", "remove"):
            raise ValueError(f"Unknown tool-reload action {action!r} (expected 'reload' or 'remove')")
        reloader = self._tool_reloaders.get(kind)
        if reloader is None:
            raise RuntimeError(
                f"No tool reloader registered for kind {kind!r} (registered: {sorted(self._tool_reloaders)})"
            )
        if inspect.iscoroutinefunction(reloader):
            result = await reloader(action, name)
        else:
            result = reloader(action, name)
        return result or {"kind": kind, "action": action, "name": name, "status": "ok"}

    def _on_shutdown(self, func: Callable):
        self._shutdown_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    async def _run_handlers(self, handlers: list[Callable], raise_on_error: bool = False):
        """Run lifecycle handlers, always attempting every handler. The shutdown
        path swallows-and-logs so teardown reaches every handler; the startup and
        reload paths pass ``raise_on_error`` so a failed handler surfaces loudly
        instead of leaving a healthy-looking half-initialized app or reporting a
        successful reload with missing tools."""
        errors: list[tuple] = []
        for handler in handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler()
                else:
                    handler()
            except Exception as e:
                logger.error(f"Error in lifecycle handler {handler.__name__}: {e}", exc_info=True)
                errors.append((handler.__name__, e))
        if raise_on_error and errors:
            raise RuntimeError("lifecycle handlers failed: " + ", ".join(f"{name}: {exc!r}" for name, exc in errors))

    async def _run_post_swap_handlers(self, *, raise_on_error: bool) -> None:
        """Run the post-swap background-loop establishers on the CURRENT serving loop.

        The caller guarantees this runs on the real serving loop with the target
        generation already installed as ``current_epoch()``: at boot inside
        ``app_context``, and after every epoch swap inside the build+swap primitive. Each
        establisher (re)starts its loop-affine loop here and registers it with the live
        generation, so the loop attaches to the serving loop and retires with its epoch.
        Boot raises on failure (a broken boot must not look healthy); a post-swap reload
        does not — the fresh epoch already serves, so a background-loop start fault is
        loud but never unwinds the completed swap."""
        await self._run_handlers(list(self._post_swap_handlers.values()), raise_on_error=raise_on_error)

    async def _run_fleet_op_applied_handlers(self, op_name: str) -> None:
        """Fire every ``on_fleet_op_applied`` handler with the op name. A raising
        handler propagates (fail-loud), turning the op's terminal reply into
        ``failed`` — the post-apply obligation is part of the op applying."""
        for handler in list(self._fleet_op_applied_handlers.values()):
            if inspect.iscoroutinefunction(handler):
                await handler(op_name)
            else:
                handler(op_name)

    def _epoch_handlers(self) -> list[Callable]:
        """The ONE ordered per-epoch handler list: the startup handlers then the
        reload handlers, de-duplicated by qualified name so a handler registered on
        BOTH hooks (the presets/sub-MCP/studio rehydration set) runs exactly once per
        epoch build. First-occurrence order is preserved, so the startup ordering —
        including presets-before-sub-MCP — holds; the reload-only handlers append
        after. Every startup handler runs per epoch (eager provider init included), so
        a rebuilt epoch's providers are instantiated before its first request."""
        return list({**self._startup_handlers, **self._reload_handlers}.values())
