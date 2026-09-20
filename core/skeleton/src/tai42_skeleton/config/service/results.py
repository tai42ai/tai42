"""The manifest-mutation pipeline's structural seams and result DTOs.

The config-manager / reload / fleet-publish Protocols the pipeline drives, and the structured outcomes
it returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from tai42_skeleton.operations._broadcast import fleet_fanout

if TYPE_CHECKING:
    from collections.abc import Callable

    from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, WorkerIdentity
    from tai42_skeleton.app.recycle import RecycleReport


class OrphanEnvWriteError(RuntimeError):
    """A combined env+manifest op whose manifest persist FAILED after the env write already landed.

    Env-first/manifest-second ordering: on a manifest-persist failure there is NO
    rollback — the env write STANDS as an inert, re-runnable orphan (rolling it back
    could leave a still-referenced marker resolving silently to ``"N/A"``). The message
    NAMES the orphan env key(s) and the manifest pointer they now reference no persisted
    marker at, and states the env write stands / re-run to complete. A ``RuntimeError``
    (not a ``ValueError``) so the op layer does not fold it into a 400 — a partial
    failure is loud, not a client input error.
    """


class _ManifestStore(Protocol):
    """The config-manager surface the pipeline drives.

    The transactional seams plus the reads the env-change validation needs. The concrete provider is the
    active :class:`~tai42_contract.config.manager.ConfigManager`.
    """

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]: ...

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]: ...

    def write_env(self, config: dict[str, str]) -> None: ...

    def replace_env(self, config: dict[str, str]) -> None: ...

    def read_env(self) -> dict[str, str]: ...

    def read_manifest_preserved(self) -> dict[str, Any]: ...


class _ReloadAdmin(Protocol):
    """The reload seam the pipeline drives on the local worker after a persist."""

    def reload_config(self) -> dict[str, Any]: ...


class _FleetPublisher(Protocol):
    """The worker-bus publish surface the pipeline broadcasts through, plus the op-start census it reads.

    The census is read BEFORE each local apply — ``publish`` censuses only when it is called, so the
    membership a report is judged against is captured through ``expected_at_start`` and handed back in.
    """

    async def expected_at_start(self) -> dict[str, int]: ...

    async def publish(
        self,
        op: dict[str, Any],
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult: ...


@dataclass(frozen=True)
class ApplyResult:
    """The structured outcome of one pipeline run.

    ``document`` is the persisted PRESERVED-view manifest (``!ENV`` markers intact)
    for :meth:`ConfigService.apply_change` / :meth:`ConfigService.apply_replace`, and
    ``None`` for :meth:`ConfigService.apply_env_change` (no manifest document changed).
    ``local`` is this worker's local reload result. ``fleet`` is the awaited per-worker
    broadcast report, which carries the two honest failure shapes itself — a reachable
    bus with an unconfirmed worker named in ``fleet.results`` (``fleet.ok`` is then
    ``False``), or a bus-unreachable result (``fleet.reachable`` is ``False`` with only
    ``fleet.error``). Both are returned successes: persist + local reload landed, and
    recovery is a fleet reload.
    """

    fleet: FleetResult
    local: dict[str, Any]
    document: dict[str, Any] | None = None

    @property
    def fanout(self) -> dict[str, Any]:
        """The mode-wrapped fan-out summary of this run's broadcast.

        The same shape :func:`~tai42_skeleton.operations._broadcast.apply_response` embeds under ``fanout``
        (local-only / fleet / unreachable). A writer that returns a bare result (not the ``apply_response``
        merge) embeds this value directly.
        """
        return fleet_fanout(self.fleet)


@dataclass(frozen=True)
class ProfileApplyOutcome:
    """The structured outcome of one profile-APPLY pipeline run.

    The raw material the operations layer folds into the dedicated ``profileApplyResponse``.

    ``hot`` is the hot-class diff key NAMES (names only — never values). ``recycle`` is
    the fleet recycle report (``None`` when the diff carried no recycle-class key, so no
    recycle rolled), each row carrying its own kind. ``self_identity`` is the applying
    worker's own identity (name + generation) — the source for the self-deferred row's
    ``generation_before``. ``serve_affecting`` is whether the applier must self-exit (arm
    the post-response graceful exit). ``fleet`` is the reload broadcast report.
    """

    hot: list[str]
    recycle: RecycleReport | None
    self_identity: WorkerIdentity
    serve_affecting: bool
    fleet: FleetResult
