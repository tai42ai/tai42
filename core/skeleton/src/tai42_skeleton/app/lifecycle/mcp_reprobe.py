"""The failed-MCP exponential-backoff re-probe background loop."""

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from tai42_contract.manifest import TaiMCPConfig

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.settings.cache import mcp_probe_timeout

logger = logging.getLogger(__name__)


class McpReprobeMixin(LifecycleState):
    """Lifecycle mixin owning the failed-MCP exponential-backoff re-probe loop."""

    def _spawn_reprobe_task(self) -> None:
        """Start the failed-MCP re-probe loop on the serving loop.

        Owned by ``app_context``; runs until cancelled at shutdown.
        """
        self._reprobe_task = asyncio.create_task(
            self._reprobe_failed_mcps_loop(),
            name="tai-failed-mcp-reprobe",
        )
        # Backstop a silent death OR an unexpected normal return loudly, mirroring
        # the worker-bus subscription: both are run-until-cancelled tasks.
        self._reprobe_task.add_done_callback(self._on_perpetual_task_done)

    async def _cancel_reprobe_task(self) -> None:
        """Cancel the re-probe task and await its termination.

        The shutdown counterpart of ``_spawn_reprobe_task``.

        A task that died with a non-``CancelledError`` exception was already
        surfaced at ERROR by its done-callback, so it is awaited-and-swallowed
        here rather than re-raised — this runs inside ``app_context``'s shutdown
        ``finally``, and re-raising would skip the remaining teardown.
        """
        task = self._reprobe_task
        self._reprobe_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: S110 task failure already surfaced by the done-callback; swallowed so shutdown completes
            # Already logged at ERROR by the done-callback; swallowed so a dead
            # re-probe task cannot abort the remaining shutdown steps.
            pass

    async def _reprobe_sleep(self, seconds: float) -> None:
        """The re-probe loop's inter-pass sleep.

        Isolated so tests can drive the backoff with a controllable clock instead of
        real time.
        """
        await asyncio.sleep(seconds)

    async def _probe_and_apply_failed(self, snapshot: dict[str, TaiMCPConfig]) -> tuple[list[dict[str, Any]], set[str]]:
        """Re-probe the snapshotted failed MCP servers OFF the reload gate, then bind the recovered ones back UNDER it.

        ``snapshot`` is the ``{title: config}`` a reprobe pass captured under the
        gate. The probe is a network read that mutates no shared state, so it runs
        off the gate and holds nothing while it waits; it is bounded by the cold-boot
        probe budget (``mcp_probe_timeout``), so a server whose start outlasts a
        rebuild's short reload budget binds here, exactly as it binds at boot and
        through the ``reload_failed_mcps`` door. The gate is re-acquired only to apply, and
        each title is re-verified STILL failed AND STILL in the manifest before its
        bind: a concurrent admin reload/deregister may have cleared or removed it
        while probing, and a stale/removed title must not be re-applied. Returns the
        per-title results and the gate-consistent still-failed set.
        """
        titles = list(snapshot)
        probes = await asyncio.gather(
            *(self._probe_mcp(snapshot[t], timeout=mcp_probe_timeout()) for t in titles),
            return_exceptions=True,
        )
        out: list[dict[str, Any]] = []
        # Every read/write of ``_failed_mcps`` (and the bind it drives) happens under
        # the reload gate; a worker-thread reload holds the same lock, so the
        # re-verify and apply below cannot race a concurrent mutation.
        async with reload_gate.synchronized():
            manifest = self._manifest
            mcp_map = (manifest.mcp_map if manifest else {}) or {}
            for title, probe in zip(titles, probes, strict=True):
                if title not in self._failed_mcps or title not in mcp_map:
                    # Cleared or removed by a concurrent admin reload/deregister while
                    # probing — a stale/removed title must not be re-applied.
                    continue
                config = snapshot[title]
                if isinstance(probe, BaseException):
                    self._record_failed_mcp(config, probe)
                    out.append({"title": title, "status": "unavailable"})
                    continue
                try:
                    out.append(await self._apply_reloaded_mcp(title, config, probe))
                except Exception:
                    # A post-probe bind/reconcile failure must not lose the other
                    # titles' results — log the trace loudly, surface this one coarsely.
                    logger.error(
                        "reload_failed_mcps: applying reloaded MCP %r failed after probe", title, exc_info=True
                    )
                    out.append({"title": title, "status": "error"})
            still_failed = set(self._failed_mcps)
        return out, still_failed

    async def _reprobe_failed_titles(self, titles: Iterable[str]) -> list[dict[str, Any]]:
        """Re-probe, now, those of ``titles`` that are failed and in the manifest; bind the recovered ones.

        The set-scoped pass the background loop runs, for named titles: the snapshot is taken under the
        reload gate, the probe runs off it on the reload budget, and the gate is taken again only to bind
        (each title re-verified still failed and still in the manifest). A named title that is bound, or
        not in the manifest, is not probed. The loop's own backoff is left as it is; a title recovered here
        is simply absent from its next snapshot. Returns the per-title results (empty when nothing was
        probed).
        """
        async with reload_gate.synchronized():
            manifest = self._manifest
            mcp_map = (manifest.mcp_map if manifest else {}) or {}
            snapshot = {t: mcp_map[t] for t in titles if t in self._failed_mcps and t in mcp_map}
        if not snapshot:
            return []
        results, _still_failed = await self._probe_and_apply_failed(snapshot)
        logger.info(
            "named failed-MCP re-probe: probed=%s recovered=%s",
            sorted(snapshot),
            sorted(r["title"] for r in results if r.get("status") == "ok"),
        )
        return results

    async def _bring_boot_credentials_up_to_date(self) -> None:
        """After boot's listing, refresh the credentials it could not, then re-probe the entries that now have one.

        Boot lists before the refresh owner is live, so a server that demands a credential whose stored
        token had expired is left failed. Once the owner is bound this brings those credentials up to date
        (one reload probe budget) and re-probes, ONCE, exactly the entries of that selection the boot
        listing left failed and that now have a credential in hand; an entry down for another reason is
        left to the ordinary re-probe loop.
        """
        in_hand = await self._bring_listed_credentials_up_to_date()
        manifest = self._manifest
        mcp_map = (manifest.mcp_map if manifest else {}) or {}
        snapshot = {
            title: mcp_map[title]
            for title, ready in in_hand.items()
            if ready and title in mcp_map and title in self._failed_mcps
        }
        if snapshot:
            results, _still_failed = await self._probe_and_apply_failed(snapshot)
            logger.info(
                "boot credential re-probe: probed=%s recovered=%s",
                sorted(snapshot),
                sorted(r["title"] for r in results if r.get("status") == "ok"),
            )

    async def _reprobe_failed_mcps_loop(self) -> None:
        """Re-probe failed-at-boot MCP servers on an exponential backoff.

        Each pass sleeps the current interval, then — only when a server is
        currently failed — SNAPSHOTS the failed titles under the reload gate, PROBES
        them off the gate on the cold-boot probe budget, and re-acquires the gate to
        rebind the recovered tools and clear them from the failed set, logging the
        outcome at INFO. The gate wraps only the brief snapshot and apply, never the
        probe: holding it across the network probe would stall every reload-gated
        write for up to the probe timeout each pass. The interval starts at
        ``mcp_reprobe_initial_seconds``, doubles (capped at
        ``mcp_reprobe_max_seconds``) after a pass where every probed server stayed
        down, and resets to the initial value after the pass that sees a recovery or
        a fresh failure. An empty failed set probes nothing.

        A cancellation (shutdown) propagates for a clean exit; any other per-pass
        error is logged loudly and the loop survives to the next pass — a silently
        dead recovery task is the exact failure mode this task removes.
        """
        interval = _lifecycle.CoreSettings().mcp_reprobe_initial_seconds
        # Titles known-failed as of the previous pass; a title present now but
        # absent here is a fresh failure and resets the backoff to probe promptly.
        # Seeded under the reload-gate lock on the first pass (below) so the
        # snapshot never races a worker-thread reload mutating the failed set.
        known_failed: set[str] | None = None
        while True:
            await self._reprobe_sleep(interval)
            try:
                settings = _lifecycle.CoreSettings()
                initial = settings.mcp_reprobe_initial_seconds
                # SNAPSHOT the failed set + each title's probe config under the
                # reload-gate lock: a worker-thread reload holds the same lock, so
                # this is the only place the failed set is read concurrency-safely (a
                # bare snapshot would race a concurrent mutation → dict-changed-size).
                # The probe that follows runs OFF the gate.
                async with reload_gate.synchronized():
                    self._refresh_manifest_mcp()
                    manifest = self._manifest
                    mcp_map = (manifest.mcp_map if manifest else {}) or {}
                    current_failed = set(self._failed_mcps)
                    if known_failed is None:
                        known_failed = current_failed
                    if not current_failed:
                        interval = initial
                        known_failed = set()
                        continue
                    newly_appeared = not current_failed <= known_failed
                    snapshot = {t: mcp_map[t] for t in current_failed if t in mcp_map}
                # Probe off the gate, then re-acquire it to bind the recovered
                # servers and read the settled failed set (both gate-consistent).
                results, still_failed = await self._probe_and_apply_failed(snapshot)
                recovered = sorted(r["title"] for r in results if r.get("status") == "ok")
                logger.info(
                    "failed-MCP re-probe pass: probed=%s recovered=%s still_failed=%s",
                    sorted(current_failed),
                    recovered,
                    sorted(still_failed),
                )
                if recovered or newly_appeared:
                    interval = initial
                else:
                    interval = min(interval * 2, settings.mcp_reprobe_max_seconds)
                known_failed = still_failed
            except Exception:
                # ``CancelledError`` is a BaseException and passes this handler for
                # a clean shutdown cancel; every other error is logged and the loop
                # continues at the current backoff.
                logger.error("failed-MCP re-probe pass failed; retrying next interval", exc_info=True)
