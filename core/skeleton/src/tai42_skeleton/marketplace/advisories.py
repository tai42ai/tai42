"""The advisory cache, its refresh, and the documented background poll.

The marketplace registry publishes security advisories per listing. This module
holds the most recent advisory snapshot for the INSTALLED plugins and keeps it
fresh two ways: on demand (:func:`current`, bounded by the configured interval)
and, when enabled, a background poll (:func:`start_poll`).

This module owns the ONLY advisory range matcher in the skeleton: the installer
never matches ranges itself (the resolve call returns advisories already matched
to the pinned version by the registry). Here, a listing's advisory applies to an
installed plugin when it is not withdrawn and its PEP 440 ``affected_versions``
specifier set contains the installed version (``prereleases=True`` so an
installed prerelease is not silently excluded).

The poll is the ONLY background outbound call the feature makes, and it is a
visible, documented setting: ``MARKETPLACE_ADVISORIES_POLL`` defaults on, the
startup emits a loud line naming the polled URL and interval, and one env var
turns it off. Under ``uvicorn --workers N`` each worker process runs its own poll
task and its own cache — N duplicate polls per interval — acceptable for this
cadence and stated deliberately.

Loop ownership matters. The poll task must live on the REAL serving loop and retire
with the generation that started it, so :func:`start_poll` is driven by the post-swap
establisher — run at boot and after every epoch swap, both ON the serving loop — and
NOT by the per-epoch handler list, which runs on a throwaway build-thread loop a
spawned task would not survive. Each (re)start registers the task with the epoch under
construction, so a retire cancels exactly that generation's own task.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict

from tai42_skeleton.marketplace.client import RegistryClient
from tai42_skeleton.marketplace.errors import ListingNotFoundError, RegistryResponseError
from tai42_skeleton.marketplace.settings import marketplace_settings
from tai42_skeleton.marketplace.store import MarketplaceInstallStore

logger = logging.getLogger(__name__)


class AdvisoryState(BaseModel):
    """A snapshot of the advisories affecting the installed plugins, with the
    UTC time it was fetched. Frozen: a served state is an immutable snapshot."""

    model_config = ConfigDict(frozen=True)

    advisories: list[dict[str, Any]]
    fetched_at: datetime


# The most recent snapshot (``None`` until the first refresh) and the running poll
# task. The task always lives on the real serving loop (the post-swap establisher runs
# there), so no remembered-loop handle is needed.
_state: AdvisoryState | None = None
_poll_task: asyncio.Task[None] | None = None


async def refresh() -> AdvisoryState:
    """Re-fetch advisories for every installed plugin and store the snapshot.

    No installs means an empty state with no registry call — there is nothing to
    ask about. Otherwise each installed listing is queried in turn (tens at most),
    keeping only non-withdrawn advisories whose ``affected_versions`` contains the
    installed version. A per-ref :class:`ListingNotFoundError` (that listing
    vanished or was suspended upstream) is per-ref state: it is skipped with a
    warning naming the ref, never failing the whole refresh. A single advisory row
    with a malformed ``affected_versions`` is likewise per-row: it is skipped with
    a warning, never aborting the refresh for every listing. A transport or
    garbled RESPONSE from the advisories call itself propagates for the caller (the
    poll loop) to log or the route to map.
    """
    installed = await MarketplaceInstallStore().list_installed()
    if not installed:
        return _store_state([])

    registry = RegistryClient()
    matched: list[dict[str, Any]] = []
    for row in installed:
        try:
            rows = await registry.advisories(listing=row.ref)
        except ListingNotFoundError:
            logger.warning("marketplace advisories: listing %s not found upstream; skipping", row.ref)
            continue
        for advisory in rows:
            if advisory.get("withdrawn_at") is not None:
                continue
            try:
                hit = _affects(advisory.get("affected_versions"), row.version)
            except RegistryResponseError:
                # One malformed advisory row (a non-PEP440 affected_versions) is
                # per-row garbled data: skip it with a loud warning rather than
                # abort the whole refresh (which would drop every listing's
                # advisories). A transport/garbled failure of the advisories CALL
                # itself still propagates above.
                logger.warning(
                    "marketplace advisories: listing %s has an advisory with a malformed affected_versions; "
                    "skipping that row",
                    row.ref,
                    exc_info=True,
                )
                continue
            if hit:
                matched.append(advisory)
    return _store_state(matched)


async def current(max_age_s: int) -> AdvisoryState:
    """The cached snapshot when it is younger than ``max_age_s``, else a fresh
    :func:`refresh`.

    The route passes ``marketplace_settings().advisories_interval_s``, so an
    operator never reads state older than the documented interval — whether or not
    the background poll is running. A refresh failure raises rather than serving
    stale data.
    """
    state = _state
    if state is not None:
        age_s = (datetime.now(UTC) - state.fetched_at).total_seconds()
        if age_s < max_age_s:
            return state
    return await refresh()


def start_poll() -> None:
    """(Re)start the background poll — the post-swap establisher body.

    Called ON the serving loop (the post-swap hook runs there at boot and after every
    epoch swap), so it cancels any previous task and — only when
    ``MARKETPLACE_ADVISORIES_POLL`` is true — logs the loud enabled line and spawns the
    task onto that loop, re-reading ``MARKETPLACE_*`` under the fresh settings caches.
    When the poll is disabled it logs nothing and starts nothing (documented silence).
    """
    task = _poll_task
    if task is not None and not task.done():
        task.cancel()
    _spawn_poll_if_enabled()


async def stop_poll() -> None:
    """Cancel and await the poll task — the shutdown-hook body.

    Called from the lifespan shutdown ON the serving loop the task lives on, so the
    await is loop-safe by construction. Only ``CancelledError`` is suppressed.
    """
    await _cancel_and_await_poll_task()


def _spawn_poll_if_enabled() -> None:
    """Spawn the poll task when the setting is on; do nothing (silently) when off.

    Must be called on the serving loop — ``create_task`` attaches the task to the
    running loop, which the module has remembered as the serving loop.
    """
    global _poll_task
    settings = marketplace_settings()
    if not settings.advisories_poll:
        _poll_task = None
        return
    logger.info(
        "marketplace advisories poll enabled: polling %s every %ss (set MARKETPLACE_ADVISORIES_POLL=false to disable)",
        settings.url,
        settings.advisories_interval_s,
    )
    task = asyncio.create_task(_poll_loop(), name="tai-marketplace-advisories")
    task.add_done_callback(_on_poll_done)
    _poll_task = task
    _register_task_with_epoch(task)


def _register_task_with_epoch(task: asyncio.Task[None]) -> None:
    """Register this poll task's cancel with the epoch under construction, so the epoch
    retire cancels exactly the generation's own task and no timer outlives its epoch. A
    no-op when no epoch is installed (a bare unit context). The cancel awaits the task
    only when it lives on the loop the retire runs on, so a task left on a torn-down
    build loop is cancelled without a cross-loop await."""
    from tai42_skeleton.app.epoch import epoch_under_construction_or_none

    epoch = epoch_under_construction_or_none()
    if epoch is None:
        return

    async def _cancel() -> None:
        if task.done():
            return
        task.cancel()
        if task.get_loop() is asyncio.get_running_loop():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    epoch.register_periodic_loop(_cancel)


async def _cancel_and_await_poll_task() -> None:
    """Cancel the running poll task and await it, suppressing only its
    ``CancelledError``."""
    global _poll_task
    task = _poll_task
    _poll_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _on_poll_done(task: asyncio.Task[None]) -> None:
    """Surface an unexpected poll-task death at ERROR; a cancellation (the normal
    stop) stays silent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("marketplace advisories poll task died unexpectedly", exc_info=exc)


async def _poll_loop() -> None:
    """Refresh advisories every interval; one failed poll logs and continues.

    A single unreachable poll must never kill the loop. A fresh snapshot carrying
    high or critical advisories logs each at WARNING naming the listing, severity,
    and summary.
    """
    settings = marketplace_settings()
    while True:
        await asyncio.sleep(settings.advisories_interval_s)
        try:
            state = await refresh()
        except Exception:
            logger.warning(
                "marketplace advisories poll failed for %s; retrying next interval", settings.url, exc_info=True
            )
            continue
        for advisory in state.advisories:
            if advisory.get("severity") in ("high", "critical"):
                logger.warning(
                    "marketplace advisory affects an installed plugin: %s severity=%s: %s",
                    advisory.get("listing"),
                    advisory.get("severity"),
                    advisory.get("summary"),
                )


def _affects(affected_versions: Any, version: str) -> bool:
    """Whether ``version`` falls in an advisory's ``affected_versions`` specifier
    set. ``prereleases=True`` so an installed prerelease is matched against a range
    (matching one exact version otherwise skips prerelease-preference semantics). A
    malformed ``affected_versions`` is garbled registry data →
    :class:`RegistryResponseError`."""
    try:
        specifier = SpecifierSet(affected_versions)
        return specifier.contains(version, prereleases=True)
    except (InvalidSpecifier, TypeError, AttributeError) as exc:
        # ``affected_versions`` is registry advisory data typed ``Any`` (this listing
        # path has no resolve-boundary type guard), so it may be a malformed
        # specifier string or a non-string JSON value, failing at either line:
        # a malformed string raises InvalidSpecifier at construction; a non-iterable
        # (a number/bool/null) raises TypeError at construction; an iterable JSON
        # array/object constructs fine and only raises AttributeError inside
        # ``.contains()``. Both lines sit under this guard so any garbled advisory is
        # a typed RegistryResponseError (502), never an untyped 500.
        raise RegistryResponseError(
            f"registry advisory has an invalid affected_versions {affected_versions!r}: {exc}", status=None
        ) from exc


def _store_state(advisories: list[dict[str, Any]]) -> AdvisoryState:
    """Store and return a fresh snapshot stamped with the current UTC time."""
    global _state
    _state = AdvisoryState(advisories=advisories, fetched_at=datetime.now(UTC))
    return _state
