"""Targeted single-MCP reload/deregister and the dependent-preset reconcile."""

import asyncio
import logging
from typing import Any

from tai42_contract.manifest import TaiMCPConfig

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.connectors.token_injection import evict_pooled_session
from tai42_skeleton.tools import mcp_health
from tai42_skeleton.tools.adapters.mcp_tool_to_func import _detect_transport

logger = logging.getLogger(__name__)


class McpReloadMixin(LifecycleState):
    async def _reload_mcp_async(self, title: str) -> dict[str, Any]:
        self._refresh_manifest_mcp()
        manifest = self._manifest
        mcp_map = (manifest.mcp_map if manifest else {}) or {}
        if title not in mcp_map:
            return {
                "title": title,
                "status": "error",
                "error": f"Unknown MCP '{title}' — not present in the current manifest.",
            }

        config = mcp_map[title]
        try:
            tools = await self._probe_mcp(config)
        except Exception as e:
            self._record_failed_mcp(config, type(e).__name__)
            return {"title": title, "status": "unavailable"}

        return await self._apply_reloaded_mcp(title, config, tools)

    async def _apply_reloaded_mcp(self, title: str, config: TaiMCPConfig, tools: list[Any]) -> dict[str, Any]:
        """Bind a freshly-probed MCP server's tools and reconcile dependent presets —
        the registry-mutating half of a reload, split from the probe.

        This half synchronously rewrites the process-wide tool registry and then
        marshals the preset reconcile onto the serving loop, so two servers applying
        at once would race each other's registry mutation across the probe and serving
        threads. ``_reload_failed_mcps_async`` therefore probes servers concurrently
        but calls this ONE server at a time; the single-server reload calls it once.
        """
        # Heal the connection this reload manages: drop the pooled dispatch session
        # for this title so the next dispatch builds a fresh one. The probe passed on
        # a throwaway off-pool client, so a dead pooled session survives it and every
        # dispatch keeps hitting the corpse until it is evicted here.
        await self._evict_mcp_session_on_serving_loop(config)

        # Clean reload: drop any tools this MCP previously bound, then rebind.
        old_bound = set(self._mcp_bound_tools.get(title, set()))
        for name in sorted(old_bound):
            try:
                self._fast_mcp.local_provider.remove_tool(name)
            except Exception:
                logger.warning("reload_mcp: could not remove stale tool %s", name, exc_info=True)

        self._mcp_tools(config, tools)
        new_bound = set(self._mcp_bound_tools.get(title, set()))

        # Symmetry with _deregister_mcp: a tool this MCP no longer serves must ALSO
        # leave the base registry, or a re-probed server drops T from the wire while
        # T lingers in _requested_tools/_tools (its self-entry keeps it out of
        # missing_tools) — a registry-derived surface stays stale until a full
        # reload.
        for name in sorted(old_bound - new_bound):
            self._tool_registry.unregister_tool_base(name)

        await self._reconcile_after_mcp_reload(title, old_bound, new_bound)

        self._failed_mcps.pop(title, None)
        result: dict[str, Any] = {
            "title": title,
            "status": "ok",
            "tools": sorted(new_bound),
        }
        # A name the (re)bind refused because a registered preset owns it — surfaced
        # loudly so the caller sees the returning server did NOT clobber the preset.
        conflicts = sorted(self._mcp_preset_conflicts.get(title, set()))
        if conflicts:
            result["preset_conflicts"] = conflicts
        return result

    async def _reconcile_after_mcp_reload(self, title: str, old_bound: set[str], new_bound: set[str]) -> None:
        """Reconcile dependent presets after a targeted MCP reload rebinds
        ``title``'s tools and the vanished-tool unregistration has settled the base
        registry.

        A preset over a base this MCP rebound is a ``TransformedTool`` whose closure
        still holds the OLD wrapper/config, so it is re-registered from its in-memory
        spec to track the freshly-bound base; a preset whose base vanished across the
        reload is quarantined (its store row surfaces as ``conflicted``).
        ``old_bound`` / ``new_bound`` are the tool names this MCP bound before and
        after the rebind,
        so their union is exactly the set of bases whose bindings changed."""
        await self._reconcile_bases_on_serving_loop(old_bound | new_bound)

    async def _reconcile_bases_on_serving_loop(self, affected_bases: set[str]) -> None:
        """Reconcile base-dependent presets with the ``PresetManager`` per-name locks
        taken on the serving loop.

        Those locks are ``asyncio.Lock``s, valid on a single loop only. The preset
        mutation routes take them on the serving loop, so every reconcile takes them
        there too: a route mutating a preset and a reconcile touching the same name
        contend on ONE loop, never two (a cross-loop contended acquire raises, and a
        lock touched from two threads gives no mutual exclusion). The reprobe pass
        already runs this ON the serving loop and awaits directly; an admin
        reload/deregister runs its body on a ``run_blocking`` worker loop and
        marshals the coroutine back onto the serving loop, awaiting the cross-loop
        result without blocking the worker loop. With no serving loop bound (a
        pure-sync boot) nothing else contends, so the reconcile runs on the current
        loop.
        """
        loop = self._serving_loop
        if loop is None or asyncio.get_running_loop() is loop:
            await self.preset_manager.reconcile_bases(affected_bases)
            return
        future = asyncio.run_coroutine_threadsafe(self.preset_manager.reconcile_bases(affected_bases), loop)
        await asyncio.wrap_future(future)

    async def _evict_mcp_session_on_serving_loop(self, config: TaiMCPConfig) -> None:
        """Evict the pooled dispatch session for ``config`` on the serving loop.

        The ``FastMCPClient`` pools are per event loop and dispatch runs on the
        serving loop, so an eviction on any other loop misses the real pool and
        leaves the dead session in place. An admin reload runs this body on a
        ``run_blocking`` worker loop and marshals the eviction back onto the
        serving loop; the reprobe pass already runs on the serving loop and awaits
        directly, and a pure-sync boot (no serving loop bound) runs it on the
        current loop. The transport is detected once here and reused, never
        re-derived. A failed eviction propagates — it must not silently leave a
        corpse in the pool.
        """
        transport = _detect_transport(config.config)

        async def _evict() -> None:
            await evict_pooled_session(config, transport, _lifecycle.FastMCPClient())

        loop = self._serving_loop
        if loop is None or asyncio.get_running_loop() is loop:
            await _evict()
            return
        future = asyncio.run_coroutine_threadsafe(_evict(), loop)
        await asyncio.wrap_future(future)

    async def _reload_failed_mcps_async(self) -> list[dict[str, Any]]:
        """Re-probe every currently-failed MCP concurrently, then apply the binds
        ONE server at a time.

        Probing is network-bound and mutates no shared state, so all servers are
        probed at once — N down servers cost ~one probe timeout, not N. Applying a
        probe result rewrites the process-wide tool/preset registry and marshals the
        reconcile onto the serving loop, so applications are serialized: running two
        at once would let one server's bind race another's reconcile across the probe
        and serving threads. Titles are snapshotted before any mutation.
        """
        self._refresh_manifest_mcp()
        manifest = self._manifest
        mcp_map = (manifest.mcp_map if manifest else {}) or {}
        titles = list(self._failed_mcps.keys())
        known = [t for t in titles if t in mcp_map]
        unknown = [t for t in titles if t not in mcp_map]

        probes = await asyncio.gather(*(self._probe_mcp(mcp_map[t]) for t in known), return_exceptions=True)

        out: list[dict[str, Any]] = []
        for title, probe in zip(known, probes, strict=True):
            config = mcp_map[title]
            if isinstance(probe, BaseException):
                self._record_failed_mcp(config, type(probe).__name__)
                out.append({"title": title, "status": "unavailable"})
                continue
            try:
                out.append(await self._apply_reloaded_mcp(title, config, probe))
            except Exception:
                # A post-probe bind/reconcile failure must not lose the other titles'
                # results — log the trace loudly, then surface this one coarsely.
                logger.error("reload_failed_mcps: applying reloaded MCP %r failed after probe", title, exc_info=True)
                out.append({"title": title, "status": "error"})
        out.extend(
            {
                "title": title,
                "status": "error",
                "error": f"Unknown MCP '{title}' — not present in the current manifest.",
            }
            for title in unknown
        )
        return out

    def _raise_if_on_serving_loop(self, op: str) -> None:
        """Refuse a reconcile-driving admin call issued from a coroutine already on
        the serving loop.

        ``reload_mcp`` / ``reload_failed_mcps`` / ``deregister_mcp`` run their async
        body through ``run_blocking`` and marshal the preset reconcile back onto the
        serving loop. Called from the serving loop itself, ``run_blocking`` would
        freeze that loop on its blocking wait, so the marshaled reconcile could never
        run — a silent deadlock. Raise loudly instead. The supported callers —
        ``reload_gate.run``'s worker thread and a loop-less sync caller — have no
        running loop here (or a different one) and pass through.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        if running is self._serving_loop:
            raise RuntimeError(
                f"{op} must not be called from the serving loop; drive it through "
                "reload_gate.run(...) or an off-loop/sync caller"
            )

    def _reload_mcp(self, title: str) -> dict[str, Any]:
        """Re-probe one MCP server by title and, if viable, (re)attach its tools.
        Synchronous and context-agnostic, mirroring ``update``.

        On failure the server is re-recorded in ``list_failed_mcps`` and its
        existing tools are left intact (transient blips self-heal). Only a
        successful reload replaces tools; a mid-rebind failure can leave the set
        partially updated — rerun after fixing the config.
        """
        self._raise_if_on_serving_loop("reload_mcp")
        return run_blocking(lambda: self._reload_mcp_async(title))

    def _reload_failed_mcps(self) -> list[dict[str, Any]]:
        """Re-probe every MCP currently in the failed list; attach the ones
        that are now viable. Synchronous and context-agnostic."""
        self._raise_if_on_serving_loop("reload_failed_mcps")
        return run_blocking(self._reload_failed_mcps_async)

    def _deregister_mcp(self, title: str) -> dict[str, Any]:
        """Detach one MCP server's tools — the removal counterpart of
        ``reload_mcp``. Idempotent: a process that never bound the title
        reports ``absent``, not an error."""
        self._raise_if_on_serving_loop("deregister_mcp")
        self._refresh_manifest_mcp()
        bound = sorted(self._mcp_bound_tools.pop(title, set()))
        failed = self._failed_mcps.pop(title, None) is not None
        # The title ceases to exist here — clear its passive health so a removed
        # MCP leaves no residue behind in the process-wide store.
        mcp_health.forget(title)
        if not bound and not failed:
            return {"title": title, "status": "absent"}
        for name in bound:
            try:
                self._fast_mcp.local_provider.remove_tool(name)
            except Exception:
                logger.warning("deregister_mcp: could not remove tool %s", name, exc_info=True)
            self._tool_registry.unregister_tool_base(name)
        # Reconcile presets that depended on the just-removed bases: a dependent
        # preset is quarantined (its store row surfaces as ``conflicted``) — no
        # dependent preset is left bound to a base that no longer exists.
        # This method is synchronous (context-agnostic, mirroring ``update``), so the
        # async reconciliation runs through the off-loop blocking runner, which
        # marshals the ``PresetManager`` locks onto the serving loop.
        if bound:
            run_blocking(lambda: self._reconcile_bases_on_serving_loop(set(bound)))
        return {"title": title, "status": "ok", "removed": bound}
