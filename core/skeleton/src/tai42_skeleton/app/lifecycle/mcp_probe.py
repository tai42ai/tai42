"""Probe manifest MCP servers and track the failed set + live MCP status."""

import asyncio
import concurrent.futures
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from tai42_contract.errors import ClientConnectError
from tai42_contract.manifest import TaiMCPConfig
from tai42_contract.manifest.errors import McpListingUnavailableCause, McpListingUnavailableError

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.connectors.managed_listing import ManagedListing, list_with_managed_auth
from tai42_skeleton.connectors.runtime.credentials_step import bring_credentials_up_to_date
from tai42_skeleton.connectors.runtime.http_status import exception_chain, mcp_http_status
from tai42_skeleton.manifest import selection_admits
from tai42_skeleton.presets.errors import PresetStoreReadError
from tai42_skeleton.settings.cache import mcp_probe_timeout, mcp_reload_probe_timeout
from tai42_skeleton.settings.mcp_settings import mcp_dispatch_settings
from tai42_skeleton.tools import mcp_health
from tai42_skeleton.tools.mcp_start import McpStartPending, StartId, outcome_text, start_listing
from tai42_skeleton.tools.naming import normalized_name

if TYPE_CHECKING:
    import mcp

    from tai42_skeleton.app.serving_core import ServingCore

logger = logging.getLogger(__name__)

# The HTTP statuses that mark a probe failure as a CREDENTIAL problem, not an outage.
_AUTH_STATUSES = frozenset({401, 403})

# Exception types (matched anywhere in the cause chain) that mark a probe failure as
# the server being UNREACHABLE — a transport/connect error or a timeout — rather than a
# reachable server answering with an error. ``httpx.TransportError`` covers connect,
# read, write and pool timeouts; ``asyncio.TimeoutError`` is a start outrunning the
# start budget.
_UNREACHABLE_EXC: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    ConnectionError,
    httpx.TransportError,
    ClientConnectError,
)


def _mcp_failure_category(exc: BaseException, http_status: int | None) -> str:
    """A coarse, credential-free category for a failed server.

    One of ``preset_store`` / ``auth`` / ``unreachable`` / ``starting`` / ``error``.

    ``preset_store`` when the preset store could not be read while the server's tools were
    being bound; ``auth`` for a 401/403 (a credential problem, not an outage); ``unreachable`` for a
    transport/connect error or a start that outran the start budget, with no HTTP status;
    ``starting`` for a start still in progress when its caller stopped waiting; ``error`` for
    any other HTTP status the server answered with, or an otherwise-unclassified failure.
    """
    if any(isinstance(err, PresetStoreReadError) for err in exception_chain(exc)):
        return "preset_store"
    if isinstance(exc, McpStartPending):
        return "starting"
    if http_status in _AUTH_STATUSES:
        return "auth"
    if http_status is not None:
        return "error"
    if any(isinstance(err, _UNREACHABLE_EXC) for err in exception_chain(exc)):
        return "unreachable"
    return "error"


def _unbound_reason(core: "ServingCore", title: str, name: str) -> str:
    """Why the selected, listed tool ``name`` of server ``title`` is not bound in the serving generation ``core``.

    ``preset_conflict`` when the bind refused it because a stored preset owns its name,
    ``unusable_schema`` when the bind skipped it because its input schema cannot be adapted, and
    ``pending_bind`` when no bind of the title has applied the live row's selection to it yet: a
    bind of the title is in progress, or the row was re-read into the live manifest after the
    title's bind and binds it at the title's next reload.
    """
    if name in core._mcp_preset_conflicts.get(title, ()):
        return "preset_conflict"
    if name in core._mcp_unusable_tools.get(title, ()):
        return "unusable_schema"
    return "pending_bind"


def _listing_unavailable(title: str, record: dict[str, Any] | None) -> McpListingUnavailableError:
    """The error of a listing read of ``title`` with no listing held, from its failed ``record`` or ``None``."""
    if record is None:
        return McpListingUnavailableError(
            title, "not_loaded", f"MCP server {title!r} is not loaded on this worker; a reload binds it"
        )
    cause: McpListingUnavailableCause = "starting" if record["category"] == "starting" else "failed"
    return McpListingUnavailableError(title, cause, record["message"])


class McpProbeMixin(LifecycleState):
    """Lifecycle mixin that probes manifest MCP servers and tracks their binding health."""

    async def _probe_mcp(self, config: TaiMCPConfig, *, wait: float | None = None) -> list["mcp.types.Tool"]:
        """Connect to one MCP server and list its tools, waiting ``wait`` for the start.

        The start runs as a task of the serving loop (:func:`start_listing`), bounded by the start
        budget ``mcp_probe_timeout``; ``wait`` bounds only this caller and defaults to the
        whole start budget. A start that ends within the wait returns its tools or raises its
        error, and callers decide whether to skip-and-record or surface it. A start still in
        progress when the wait ends raises :class:`McpStartPending`; the caller stopping its
        wait does not tear the start down — only the start budget does — and its outcome is
        applied on the serving loop when it arrives (:meth:`_apply_late_probe`). With no
        serving loop bound, :func:`start_listing` waits the start out whatever the wait, so
        the probe never answers with a pending start there. The listing goes
        through the managed-credential seam (:func:`list_with_managed_auth`): the request
        without a credential first, a credential only when the server answers it 401; the
        start budget bounds the whole seam. Each attempt is a one-shot, off-pool
        ``FastMCPClient`` so no raw fastmcp ``Client`` is opened by the app. Whether the
        server demanded a credential is noted for the title whatever the outcome, in the
        serving generation that launched the start (:func:`_note_credential_demand`).

        One start of a server runs at a time in a serving generation: a probe of the same
        ``config`` while a start of it launched in this generation is in flight joins that
        start (its key is the generation and the config), whichever door launched it — boot,
        a rebuild, a reload door or a re-probe — and gets its outcome, or the pending that names
        it. A new serving generation launches its own start: its failed records and its
        credential-demand notes are its own, and an older generation's start is never applied to
        its records (:meth:`_apply_probe_outcome`).
        """

        def _open(effective: TaiMCPConfig):
            return self.clients.client_ctx(_lifecycle.FastMCPClient, fresh=True, config=effective.model_dump())

        async def _list(client: Any) -> list["mcp.types.Tool"]:
            return await client.list_tools()

        # Resolved now: a start may end after the serving generation was replaced.
        core = self._serving_core
        demanded_titles = core._mcp_credential_demanded

        async def _do() -> list["mcp.types.Tool"]:
            listing = ManagedListing(open_client=_open)
            try:
                return await list_with_managed_auth(config, listing, _list)
            finally:
                _note_credential_demand(demanded_titles, config.title, demanded=listing.credential_demanded)

        return await start_listing(
            _do,
            title=config.title,
            key=(core, config.model_dump_json()),
            wait=wait,
            lifetime=mcp_probe_timeout(),
            on_late_outcome=self._apply_late_probe(config),
        )

    def _apply_late_probe(self, config: TaiMCPConfig) -> Callable[[StartId, Any], None]:
        """The hook that applies the outcome of a start ``_probe_mcp`` stopped waiting for.

        Called on the start's loop when the start ends; it schedules the outcome onto the
        serving loop, where, under the reload gate, :meth:`_apply_late_outcome`
        applies it only when the title's ``starting`` record waits on that very start.
        Two starts of one title overlap only across serving generations or configs (a
        rebuild, or an edit, while a late start is pending); the record waits on the one
        recorded last, so an older start's outcome never replaces it
        (:meth:`_apply_probe_outcome`). A start that ends once no serving loop is bound or
        running (``app_context`` has unbound it) leaves nothing to apply on: logged at ERROR.
        """
        title = config.title

        def apply(start: StartId, outcome: Any) -> None:
            loop = self.serving_loop()
            if loop is None or not loop.is_running():
                logger.error(
                    "MCP server '%s' finished starting after its caller stopped waiting, but no serving loop "
                    "is running to apply the outcome (%s)",
                    title,
                    outcome_text(outcome),
                )
                return
            future = asyncio.run_coroutine_threadsafe(self._apply_late_outcome(config, start, outcome), loop)
            future.add_done_callback(lambda done: _log_late_apply_failure(title, done))

        return apply

    async def _apply_late_outcome(self, config: TaiMCPConfig, start: StartId, outcome: Any) -> None:
        """On the serving loop, under the reload gate: apply the late outcome of ``start``.

        It applies, through :meth:`_apply_probe_outcome`, only when the title's failed
        record waits on ``start``; otherwise it is logged with the reason and dropped.
        """
        async with reload_gate.synchronized():
            result = await self._apply_probe_outcome(config.title, config, outcome, awaited=start)
        if result is not None:
            logger.info("MCP server '%s' late start applied: %s", config.title, result["status"])

    def _listed_mcp_tools(self, config: TaiMCPConfig) -> list[tuple[str, "mcp.types.Tool"]]:
        """Every tool in the listing this worker bound the server of ``config`` from, paired with its served name.

        Reads the live serving generation's retained listing and never starts the server. With
        no listing held for the title, raises :class:`McpListingUnavailableError` whose cause is
        read from the title's failed record: ``starting`` for a ``starting`` record, ``failed`` for
        any other record (its recorded message), ``not_loaded`` when the generation holds none.
        """
        core = self._live_serving_core
        listing = core._mcp_listed_tools.get(config.title)
        if listing is None:
            raise _listing_unavailable(config.title, core._failed_mcps.get(config.title))
        return [(normalized_name(config.title, tool.name), tool) for tool in listing]

    async def _served_mcp_names(self, config: TaiMCPConfig) -> list[str]:
        """The served names of the adaptable tools in the listing this worker bound ``config``'s server from.

        A tool whose input schema the binder cannot adapt is left out. Raises
        :class:`McpListingUnavailableError` when this worker holds no listing for the title.
        """
        # Imported here: the binding package imports this lifecycle package.
        from tai42_skeleton.tools.binding.registration import adapt_mcp_tool

        pairs = self._listed_mcp_tools(config)
        # Resolved once, outside the per-tool guard, so a malformed depth setting raises.
        schema_max_depth = mcp_dispatch_settings().schema_max_depth
        served: list[str] = []
        for name, tool in pairs:
            try:
                adapt_mcp_tool(config, tool, name, schema_max_depth)
            except Exception:
                # The binder's own unusable-schema skip: the tool binds on no worker.
                logger.info("mcp %r tool %r is not served: unusable input schema", config.title, name, exc_info=True)
                continue
            served.append(name)
        return served

    async def _advertised_mcp_tools(self, title: str) -> list[dict[str, Any]]:
        """Every tool in the listing this worker bound MCP server ``title`` from, with its bind state.

        Rows cover the whole listing, before the selection, and ``selected`` is the live row's
        selection. Reads the live serving generation and never starts the server, and is not
        reload-gated: a read during a bind of the title answers that bind's records as they stand.
        ``unbound_reason`` is set only for a selected row that is not bound (:func:`_unbound_reason`).
        Raises ``LookupError`` for a title not in the live manifest and
        :class:`McpListingUnavailableError` when this worker holds no listing for the title.
        """
        core = self._live_serving_core
        manifest = core._manifest
        config = next((cfg for cfg in manifest.mcp if cfg.title == title), None) if manifest is not None else None
        if config is None:
            raise LookupError(f"unknown mcp title {title!r}")
        pairs = self._listed_mcp_tools(config)
        # Snapshot-copied: this read may run on a backend job's thread while a bind on the
        # serving loop adds to the set.
        bound_names = set(core._mcp_bound_tools.get(title, ()))
        rows: list[dict[str, Any]] = []
        for name, tool in pairs:
            selected = selection_admits(name, config.include, config.exclude)
            bound = name in bound_names
            rows.append(
                {
                    "name": name,
                    "title": tool.title,
                    "description": tool.description,
                    "selected": selected,
                    "bound": bound,
                    "unbound_reason": _unbound_reason(core, title, name) if selected and not bound else None,
                }
            )
        return sorted(rows, key=lambda row: row["name"])

    def _start_awaited_by(self, title: str) -> StartId | None:
        """The start the failed record of ``title`` waits on: set for a ``starting`` record, else ``None``."""
        record = self._failed_mcps.get(title)
        return record.get("start") if record is not None else None

    async def _bring_listed_credentials_up_to_date(self) -> dict[str, bool]:
        """Bring up to date the credentials of the live entries whose server demanded one at their last listing.

        Returns, per selected title, whether a credential is now in hand (see
        :func:`bring_credentials_up_to_date`); bounded by one ``mcp_reload_probe_timeout``.
        """
        manifest = self._manifest
        if manifest is None or not manifest.mcp:
            return {}
        demanded = set(self._mcp_credential_demanded)
        entries = [config for config in manifest.mcp if config.title in demanded]
        return await bring_credentials_up_to_date(entries, timeout=mcp_reload_probe_timeout())

    async def _load_mcps(
        self,
    ) -> tuple[list[tuple[TaiMCPConfig, Any]], list[tuple[TaiMCPConfig, BaseException]]]:
        """Probe every manifest MCP server concurrently, each isolated.

        Returns ``(successes, failures)`` and never raises for one server, so a
        dead/slow MCP can't abort startup.
        """
        manifest = self._manifest
        if manifest is None or not manifest.mcp:
            return [], []

        # A RELOAD holds the reload gate while the fleet is live, so a slow or unreachable
        # server must not hold the rebuild for the whole start budget — it would stall every
        # reload-gated write and fleet-reload convergence. A rebuild waits only the short reload
        # budget; a start still in progress then is recorded ``starting`` and goes on, as a task
        # of the serving loop, up to the start budget, binding through the late-outcome apply when
        # it completes; with no serving loop bound, the start is waited out instead
        # (:func:`start_listing`). The one-time cold boot waits the whole start budget.
        wait = mcp_reload_probe_timeout() if _lifecycle.is_epoch_rebuild_in_progress() else mcp_probe_timeout()

        async def run_one(config: TaiMCPConfig):
            try:
                tools = await self._probe_mcp(config, wait=wait)
            except Exception as e:
                return config, None, e
            else:
                return config, tools, None

        results = await asyncio.gather(*(run_one(cfg) for cfg in manifest.mcp))
        successes, failures = [], []
        for config, tools, error in results:
            if error is None:
                successes.append((config, tools))
            else:
                failures.append((config, error))
        return successes, failures

    def _record_failed_mcp(self, config: TaiMCPConfig, exc: BaseException) -> None:
        """Record a failed MCP as ``unavailable`` with a credential-free failure detail, and log it.

        The shared seam of every failed-probe door (boot ``_load_mcps``, the reprobe
        loop, and the ``reload_mcp`` / ``reload_failed_mcps`` doors), so what a failure
        records is decided in one place. Stores the coarse ``unavailable`` status, a
        credential-free ``category`` (``preset_store`` / ``auth`` / ``unreachable`` /
        ``starting`` / ``error``), the redacted exception ``message``
        (``mcp_health.redact_probe_error`` strips URL-embedded credentials, the same way the
        dispatch-health record does) and the ``http_status`` the exception carries when it
        has one. The config itself is never stored — ``list_failed_mcps`` is LLM-callable and
        the config carries credentials — and the exception CLASS goes only to the operator
        log, not the LLM-callable record. So a 401 reads as ``auth``, never as an outage. A
        failure logs at ERROR. A ``starting`` record logs at INFO and its message adds that
        the server binds once its start lists its tools: every door that records a failure
        probes through :meth:`_probe_mcp`, whose late outcome is applied to the failed title.
        A ``starting`` record keeps the start the pending names under ``start``: the record
        waits on that start, and no older start's outcome replaces it
        (:meth:`_apply_probe_outcome`); the listing leaves ``start`` out.
        """
        http_status = mcp_http_status(exc)
        category = _mcp_failure_category(exc, http_status)
        message = mcp_health.redact_probe_error(str(exc))
        if isinstance(exc, McpStartPending):
            message = f"{message}; it binds once its start lists its tools"
        record: dict[str, Any] = {
            "status": "unavailable",
            "category": category,
            "message": message,
            "http_status": http_status,
        }
        if isinstance(exc, McpStartPending):
            record["start"] = exc.start
        self._failed_mcps[config.title] = record
        if category == "starting":
            # Not a failure: the start goes on, and its outcome is applied to this record.
            logger.info(
                "MCP server '%s' still starting, recorded starting until its start completes: %s",
                config.title,
                message,
            )
            return
        logger.error(
            "MCP server '%s' unavailable — skipped, recorded for reload (category=%s class=%s http_status=%s): %s",
            config.title,
            category,
            type(exc).__name__,
            http_status,
            message,
        )

    def _missing_tools_ignore(self) -> frozenset[str]:
        """Return the tool names an MCP server was to provide that are legitimately absent.

        Three kinds, so ``tools.validation`` must not raise for them: the names the failed
        MCP servers were to provide (the servers are down); the names the binder left to a
        stored preset (the preset binds the name when the stored presets register); and the
        include names of a bound server that it did not bind for any other reason — the
        server's listing did not carry them (the advertised-tools read reports them as
        ``unadvertised``) or the binder skipped them for an unusable input schema (logged
        loudly at the bind and reported as ``unusable_schema``) — unless a ``tools`` entry
        includes the same name. The failed-server names match by base name (``:``-extension
        stripped), so validation is slightly under-strict for a missing tool sharing a base
        name with a failed MCP's tool — collisions are unlikely and not crashing wins.
        """
        ignore: set[str] = set()
        title_map = getattr(self._manifest, "include_title_mcp_tools_map", {}) or {}
        module_map = getattr(self._manifest, "include_module_tools_map", {}) or {}
        module_includes: set[str] = set().union(*module_map.values())
        for title in self._failed_mcps:
            ignore |= set(title_map.get(title, set()))
        for names in self._mcp_preset_conflicts.values():
            ignore |= names
        for title, bound in self._mcp_bound_tools.items():
            unbound = set(title_map.get(title, set())) - bound - self._mcp_preset_conflicts.get(title, set())
            ignore |= unbound - module_includes
        return frozenset(ignore)

    def _list_failed_mcps(self) -> list[dict[str, Any]]:
        """List MCP servers skipped due to a failed viability check, each with its failure detail.

        Every row carries ``title`` + the recorded record (``status``, credential-free
        ``category``, redacted ``message``, ``http_status``) — see
        :meth:`_record_failed_mcp`; the start a ``starting`` record waits on is not listed.
        No config, no unredacted text — this is LLM-callable, logged and broadcast, and the
        config carries credentials. Per-process: in a
        multi-worker backend this reflects only the current process.

        Reads race a reload a backend job drives on its own thread, mutating ``_failed_mcps`` (this read is
        deliberately not reload-gated, so status keeps answering mid-reload), so
        the dict is snapshot-copied — a single C-level op, atomic under the GIL —
        before iterating.
        """
        return [
            {"title": title, **{key: value for key, value in record.items() if key != "start"}}
            for title, record in dict(self._failed_mcps).items()
        ]

    def _live_mcp_status(self) -> dict[str, Any]:
        """Snapshot the in-process MCP-binding state.

        Returns ``{"bound": {title: [tool, ...]}, "failed": [{title, status}],
        "health": {title: {last_success, last_error, consecutive_failures,
        failing_since}}}`` (consumed by ``GET /api/mcp-status``). ``health`` covers
        every bound and failed title; a never-called MCP carries the block at its
        empty values. The four fields are this worker's passive dispatch health —
        per-process, so a fleet report reads each worker's own.

        Reads race a reload a backend job drives on its own thread, mutating ``_mcp_bound_tools`` (this
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


def _log_late_apply_failure(title: str, done: concurrent.futures.Future[None]) -> None:
    """Surface a late apply that did not complete: a failure at ERROR with its trace."""
    if done.cancelled():
        logger.info("MCP server '%s': applying its late start outcome was cancelled (the serving loop stopped)", title)
        return
    error = done.exception()
    if error is not None:
        logger.error("MCP server '%s': applying its late start outcome failed", title, exc_info=error)


def _note_credential_demand(demanded_titles: set[str], title: str, *, demanded: bool) -> None:
    """Keep, in ``demanded_titles``, whether ``title``'s server 401'd its LAST listing's credential-free request.

    ``demanded_titles`` is the set of the serving generation that launched the listing, kept beside its
    failed set; set and cleared by each listing.
    """
    if demanded:
        demanded_titles.add(title)
    else:
        demanded_titles.discard(title)
