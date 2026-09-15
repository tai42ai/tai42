"""Probe manifest MCP servers and track the failed set + live MCP status."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from tai42_contract.manifest import TaiMCPConfig

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.settings.cache import mcp_probe_timeout, mcp_reload_probe_timeout
from tai42_skeleton.tools import mcp_health

if TYPE_CHECKING:
    import mcp

logger = logging.getLogger(__name__)


class McpProbeMixin(LifecycleState):
    """Lifecycle mixin that probes manifest MCP servers and tracks their binding health."""

    async def _probe_mcp(self, config: TaiMCPConfig, timeout: float | None = None) -> list["mcp.types.Tool"]:
        """Connect to one MCP server and list its tools, bounded by ``timeout``.

        ``timeout`` defaults to the cold-boot ``mcp_probe_timeout``. Raises on
        failure/timeout; callers decide whether to skip-and-record or surface the
        error. The probe runs through the pooled ``FastMCPClient`` (one-shot,
        off-pool) so no raw fastmcp ``Client`` is opened by the app.
        """

        async def _do() -> list["mcp.types.Tool"]:
            async with self.clients.client_ctx(
                _lifecycle.FastMCPClient, fresh=True, config=config.model_dump()
            ) as client:
                return await client.list_tools()

        return await asyncio.wait_for(_do(), timeout=timeout if timeout is not None else mcp_probe_timeout())

    async def _load_mcps(
        self,
    ) -> tuple[list[tuple[TaiMCPConfig, Any]], list[tuple[TaiMCPConfig, str]]]:
        """Probe every manifest MCP server concurrently, each isolated.

        Returns ``(successes, failures)`` and never raises for one server, so a
        dead/slow MCP can't abort startup. Driven off-loop through
        ``run_blocking`` so a sync (Celery/RQ) caller and the serving loop both
        reach it safely.
        """
        manifest = self._manifest
        if manifest is None or not manifest.mcp:
            return [], []

        # A RELOAD holds the reload gate while the fleet is live, so an unreachable server
        # must not block the probe for the generous cold-boot budget — it would stall every
        # reload-gated write and fleet-reload convergence. Use the short reload budget for a
        # rebuild (the re-probe task / ``reload_failed_mcps`` door binds a laggard a moment
        # later); keep the full budget only for the one-time cold boot.
        timeout = mcp_reload_probe_timeout() if _lifecycle.is_epoch_rebuild_in_progress() else mcp_probe_timeout()

        async def run_one(config: TaiMCPConfig):
            try:
                tools = await self._probe_mcp(config, timeout=timeout)
            except Exception as e:
                return config, None, type(e).__name__
            else:
                return config, tools, None

        results = await asyncio.gather(*(run_one(cfg) for cfg in manifest.mcp))
        successes, failures = [], []
        for config, tools, kind in results:
            if kind is None:
                successes.append((config, tools))
            else:
                failures.append((config, kind))
        return successes, failures

    def _record_failed_mcp(self, config: TaiMCPConfig, kind: str) -> None:
        """Record a failed MCP as ``unavailable`` and log it.

        Stores only the title + coarse status, never the exception text or
        config — ``list_failed_mcps`` is LLM-callable and the config carries
        credentials. Only ``kind`` (exception class name) reaches the log.
        """
        self._failed_mcps[config.title] = "unavailable"
        logger.error(
            "MCP server '%s' unavailable — skipped, recorded for reload (%s)",
            config.title,
            kind,
        )

    def _missing_tools_ignore(self) -> frozenset[str]:
        """Return tool names the failed MCP servers were to provide, now legitimately absent.

        The servers are down, so ``tools.validation`` must not raise for them.
        Matched by base name (``:``-extension stripped), so validation is
        slightly under-strict for a missing tool sharing a base name with a
        failed MCP's tool — collisions are unlikely and not crashing wins.
        """
        ignore: set[str] = set()
        title_map = getattr(self._manifest, "include_title_mcp_tools_map", {}) or {}
        for title in self._failed_mcps:
            ignore |= set(title_map.get(title, set()))
        return frozenset(ignore)

    def _list_failed_mcps(self) -> list[dict[str, str]]:
        """List MCP servers skipped due to a failed viability check: ``title`` + coarse ``status`` only.

        No config, no exception text — this is LLM-callable, logged and
        broadcast, and the config carries credentials. Per-process: in a
        multi-worker backend this reflects only the current process.

        Reads race a reload worker thread mutating ``_failed_mcps`` (this read is
        deliberately not reload-gated, so status keeps answering mid-reload), so
        the dict is snapshot-copied — a single C-level op, atomic under the GIL —
        before iterating.
        """
        return [{"title": title, "status": status} for title, status in dict(self._failed_mcps).items()]

    def _live_mcp_status(self) -> dict[str, Any]:
        """Snapshot the in-process MCP-binding state.

        Returns ``{"bound": {title: [tool, ...]}, "failed": [{title, status}],
        "health": {title: {last_success, last_error, consecutive_failures,
        failing_since}}}`` (consumed by ``GET /api/mcp-status``). ``health`` covers
        every bound and failed title; a never-called MCP carries the block at its
        empty values. The four fields are this worker's passive dispatch health —
        per-process, so a fleet report reads each worker's own.

        Reads race a reload worker thread mutating ``_mcp_bound_tools`` (this
        read is deliberately not reload-gated, so status keeps answering
        mid-reload), so the dict and each per-title tool set are snapshot-copied
        — single C-level ops, atomic under the GIL — before iterating.
        """
        bound = {title: sorted(set(tools)) for title, tools in dict(self._mcp_bound_tools).items()}
        failed = self._list_failed_mcps()
        titles = set(bound) | {row["title"] for row in failed}
        return {
            "bound": bound,
            "failed": failed,
            "health": {title: mcp_health.snapshot(title) for title in sorted(titles)},
        }
