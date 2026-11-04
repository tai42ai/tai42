"""The manifest-mutation pipeline's structural seams and result DTOs.

The config-manager / reload / fleet-publish / combo-check Protocols the pipeline drives, and the
structured outcomes it returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from tai42_skeleton.app.bus.ops import FleetOp
from tai42_skeleton.operations._broadcast import fleet_fanout

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from fastmcp.tools.base import Tool
    from tai42_contract.app.responses import AwaitingWorker
    from tai42_contract.manifest import ExtensionElement, TaiMCPConfig
    from tai42_kit.settings import EnvReader

    from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, WorkerRow
    from tai42_skeleton.app.recycle import RecycleReport
    from tai42_skeleton.config.recycle_policy import RecycleDecision
    from tai42_skeleton.config.recycle_roll import RollOutcome


def joined_span_release_errors(*errors: str | None) -> str | None:
    """The failed span releases of one change's writes, joined with ``"; "`` in write order.

    ``None`` when none of them failed.
    """
    return "; ".join(error for error in errors if error is not None) or None


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

    The store's exclusive write span, the transactional seams, and the reads the env-change validation
    needs. The concrete provider is the active :class:`~tai42_contract.config.manager.ConfigManager`.
    """

    def exclusive_write(self) -> AbstractContextManager[None]: ...

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]: ...

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]: ...

    def write_env(self, config: dict[str, str]) -> None: ...

    def replace_env(self, config: dict[str, str]) -> None: ...

    def read_env(self) -> dict[str, str]: ...

    def read_manifest_preserved(self) -> dict[str, Any]: ...


class _ReloadAdmin(Protocol):
    """The reload seam the pipeline drives on the local worker after a persist."""

    async def reload_config(self) -> dict[str, Any]: ...

    async def served_mcp_names(self, config: TaiMCPConfig) -> list[str]: ...


class _ComboCheck(Protocol):
    """The dry-run combo check the validation runs on every extension combo of a candidate manifest.

    ``served_bases`` is the live serving generation's served tools by name, read once as a
    validation input; ``check_combo`` dry-run binds one combo over the ``base`` taken from it
    (``None`` for a tool the generation does not serve) and raises
    :class:`~tai42_skeleton.tools.binding.errors.ExtensionComboRefusedError` on a refusal.
    """

    async def served_bases(self) -> Mapping[str, Tool]: ...

    def check_combo(self, tool: str, combo: Sequence[ExtensionElement], *, base: Tool | None) -> None: ...


class _FleetPublisher(Protocol):
    """The worker-bus surface the pipeline publishes through, and the census and env-reader reads it judges by.

    The census is read ONCE before each local apply — ``publish`` censuses only when it is called, so
    the membership a report is judged against is derived from that read (``expected_from_census``)
    and handed back in, and the recycle targets are pinned from the same rows.
    """

    async def census(self) -> list[WorkerRow]: ...

    def expected_from_census(self, rows: Sequence[WorkerRow]) -> dict[str, int]: ...

    async def env_readers(self, digests: Sequence[str]) -> dict[str, tuple[EnvReader, ...]]: ...

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult: ...


@dataclass(frozen=True)
class PersistedChange:
    """A manifest mutation the pipeline validated, sealed and persisted; nothing has converged on it yet.

    ``document`` is the persisted PRESERVED-view manifest; ``span_release_error`` the store's
    error when its exclusive write span failed to release after the persist, ``None`` when it released.
    """

    document: dict[str, Any]
    span_release_error: str | None = None


@dataclass(frozen=True)
class ApplyResult:
    """The structured outcome of one pipeline run.

    ``document`` is the persisted PRESERVED-view manifest (``!ENV`` markers intact)
    for :meth:`ConfigService.apply_change` / :meth:`ConfigService.reapply_manifest` /
    :meth:`ConfigService.apply_replace`, and ``None`` for :meth:`ConfigService.apply_env_change`
    (no manifest document changed).
    ``local`` is this worker's local reload result. ``fleet`` is the awaited per-worker
    broadcast report, which carries the two honest failure shapes itself — a reachable
    bus with an unconfirmed worker named in ``fleet.results`` (``fleet.ok`` is then
    ``False``), or a bus-unreachable result (``fleet.reachable`` is ``False`` with only
    ``fleet.error``). Both are returned successes: persist + local reload landed, and
    recovery is a fleet reload. ``decision`` is which workers the reload report says need a
    restart (:func:`~tai42_skeleton.config.recycle_policy.recycle_decision`) and ``recycle``
    the fleet recycle roll it ran (``None`` when the roll had no worker to visit: every worker's
    reload confirmed no recycle-class key, or the only ones naming one were refused at their epoch build
    or are the writer whose own restart cannot follow as its exit, so nothing rolled).
    ``awaiting_respawn`` is the operation's verdict after the roll: the workers still awaiting a
    respawn (``operations._broadcast.awaiting_respawn_after``).
    ``span_release_error`` is the store's error when its exclusive write span failed to release
    after the persist (``<ErrorType>: <message>``), ``None`` when it released — the change stands
    and the reload, broadcast and roll above still ran.

    ``roll_deferred`` is set when the roll is not judged in this result: the run ran inside a
    :func:`~tai42_skeleton.config.service.deferred_roll.deferring_rolls` scope, its reload is
    recorded there, ``decision`` and ``recycle`` are ``None`` and ``awaiting_respawn`` is not
    judged yet, until the caller folds the scope's outcome in with :meth:`rolled`; or the run's
    roll was handed to the bus member ``roll_deferred_to`` names, which carries it out on
    its own (:attr:`~tai42_skeleton.config.recycle_roll.RollOutcome.deferred_to`), so ``recycle`` is
    ``None`` and ``awaiting_respawn`` empty. ``roll_deferred_to`` is ``None`` otherwise.
    ``self_exit_armed`` is whether the writer's own post-reply self-exit follows the roll, as the
    roll's outcome states it (:attr:`~tai42_skeleton.config.recycle_roll.RollOutcome.self_exit_armed`).
    """

    fleet: FleetResult
    local: dict[str, Any]
    document: dict[str, Any] | None = None
    decision: RecycleDecision | None = None
    recycle: RecycleReport | None = None
    awaiting_respawn: list[AwaitingWorker] = field(default_factory=list)
    span_release_error: str | None = None
    roll_deferred: bool = False
    roll_deferred_to: str | None = None
    self_exit_armed: bool = False

    def rolled(self, roll: RollOutcome | None) -> ApplyResult:
        """This result with its scope's recycle outcome folded in: the decision, the roll's report and the verdict.

        ``None`` (the scope recorded no reload) returns the result as it is.
        """
        if roll is None:
            return self
        return replace(
            self,
            decision=roll.decision,
            recycle=roll.recycle,
            awaiting_respawn=roll.awaiting_respawn,
            roll_deferred=roll.deferred,
            roll_deferred_to=roll.deferred_to,
            self_exit_armed=roll.self_exit_armed,
        )

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

    ``hot`` is the hot-class diff key NAMES (names only — never values). ``decision`` is which
    workers the reload report says need a restart
    (:func:`~tai42_skeleton.config.recycle_policy.recycle_decision`), its ``writer`` the applying
    worker's own identity (name + generation) — the source for the self-deferred row's
    ``generation_before``. ``recycle`` is the fleet recycle report (``None`` when the roll had no
    worker to visit: every worker's reload confirmed no recycle-class key, or the only ones naming
    one were refused at their epoch build or are the writer whose own restart cannot follow as its exit,
    so no recycle rolled), each row carrying its own kind. ``fleet`` is the reload
    broadcast report. ``roll_deferred`` is set when the roll is not judged in this outcome: the
    apply ran inside a :func:`~tai42_skeleton.config.service.deferred_roll.deferring_rolls` scope,
    its reload is recorded there and ``decision``, ``recycle`` and ``awaiting_respawn`` wait on the
    scope; or its roll was handed to the bus member ``roll_deferred_to`` names, which carries it
    out on its own, so ``recycle`` is ``None`` and ``awaiting_respawn`` empty.
    ``roll_deferred_to`` is ``None`` otherwise.
    ``self_exit_armed`` is whether the applier's own post-reply self-exit follows the roll, as the
    roll's outcome states it (:attr:`~tai42_skeleton.config.recycle_roll.RollOutcome.self_exit_armed`).
    ``awaiting_respawn`` is the operation's verdict after the roll: the workers still awaiting a
    respawn. ``span_release_error`` is the store's error when its exclusive write span failed to
    release after the env persisted (``<ErrorType>: <message>``), ``None`` when it released — the
    change stands and the broadcast and roll above still ran.
    """

    hot: list[str]
    decision: RecycleDecision | None
    recycle: RecycleReport | None
    fleet: FleetResult
    awaiting_respawn: list[AwaitingWorker] = field(default_factory=list)
    span_release_error: str | None = None
    roll_deferred: bool = False
    roll_deferred_to: str | None = None
    self_exit_armed: bool = False


@dataclass(frozen=True)
class FleetReloadOutcome:
    """What a fleet soft-restart did: its reload report and the recycle decision it settled.

    ``fleet`` is the reload broadcast's report; ``roll`` which of the workers it addressed need a
    restart (:func:`~tai42_skeleton.config.recycle_policy.recycle_decision`), the roll it ran
    over them, the verdict after it and whether the writer's own post-reply self-exit follows.
    """

    fleet: FleetResult
    roll: RollOutcome
