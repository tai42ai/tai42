"""Recycle policy: the keys no env change may carry, whom a fleet recycle restarts, and the orchestrator's constants.

Two kinds of key are refused by every env writer, on every deployment:

* The bus-reaching keys (:func:`bus_reaching_keys`): every env name the settings layer
  reads for the bus Redis URL or the bus namespace. The orchestrator's census rides the bus
  itself, so a bus recycle is intrinsically unobservable: the scan opens the OLD bus while
  replacements register only on the NEW bus after resync. ``TAI_DEFAULT_REDIS_URL`` reaches the
  bus via ``BusSettings``' default-URL fallback and ``TAI_BUS_REDIS`` sets the bus Redis group
  as one JSON value, so both join ``TAI_BUS_REDIS_URL``; ``TAI_BUS_NAMESPACE`` prefixes every bus
  key, the census included, so a recycled worker under a new namespace registers in a census the
  scan never reads. A name matches under any spelling the settings layer reads
  (:func:`~tai42_kit.settings.fold_env_name`).
* The deployment's own infrastructure reads (:data:`X_CLASSIFIED_DEPLOYMENT_BARE_READS`), folded
  into the boundary validator's X band.

A ``recycle``-class change is carried by a worker recycle: the recycled worker exits gracefully
and the deployment's process supervisor starts it again under the new env. Which workers a recycle
restarts is one decision (:func:`recycle_decision`), read from the workers' own reload reports: a
worker other than the writer those reports leave unknown is asked once it is ready (:func:`judge_row`
reads its reply), and the writer, never asked at its own roll, needs its restart when its own state is
unknown.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Set
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tai42_kit.settings import fold_env_name

from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerIdentity, WorkerKind, WorkerResult
from tai42_skeleton.app.bus_settings import fleet_env_names
from tai42_skeleton.app.discarded_build import states_discarded_build

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


def bus_reaching_keys(keys: Iterable[str]) -> list[str]:
    """The keys of ``keys`` that reach the bus, refused on every deployment, sorted.

    A key reaches the bus when the settings layer reads it, under any spelling, for a value that
    locates the bus and its census (:func:`~tai42_skeleton.app.bus_settings.fleet_env_names`).
    """
    names = fleet_env_names()
    return sorted({key for key in keys if fold_env_name(key) in names})


# Deployment-infrastructure bare reads. X-classified: no profile may carry them — a carried
# value would relocate the readiness sentinel. The boundary validator folds this set into its
# X-band refusal, enforced at EVERY env writer.
X_CLASSIFIED_DEPLOYMENT_BARE_READS: frozenset[str] = frozenset({"TAI_READY_SENTINEL_PATH"})

# The worker kinds the recycle orchestrator censuses as recycle targets.
CENSUS_TARGET_KINDS: tuple[WorkerKind, ...] = (WorkerKind.backend, WorkerKind.serve)


def _replace_diff_keys(stored: Mapping[str, str], proposed: Mapping[str, str]) -> set[str]:
    """The env key NAMES a whole-env replace changes: added, removed, or value-changed.

    Names only — the caller classifies them; a diff never carries a value off this seam.
    """
    added = {key for key in proposed if key not in stored}
    removed = {key for key in stored if key not in proposed}
    changed = {key for key in proposed if key in stored and stored[key] != proposed[key]}
    return added | removed | changed


def _refuse_unrecyclable(diff_keys: set[str], *, change: str) -> None:
    """Refuse an env change whose diff carries a bus-reaching key.

    A bus URL and the bus namespace locate the worker bus and its census, which no recycle can
    carry. The refusal is a loud ``ValueError`` the operations layer maps to a 400, naming
    ``change`` (what is being applied, e.g. ``"this settings profile"``) and the keys. Names only.
    """
    bus = bus_reaching_keys(diff_keys)
    if bus:
        raise ValueError(
            f"Refusing to apply {change}: it changes bus-reaching key(s): {', '.join(bus)}. A bus URL and the "
            "bus namespace locate the worker bus and its census, which no recycle can carry. Set them in the "
            "deployment's environment and restart the deployment."
        )


def recycle_step_timeout() -> float:
    """The per-step recycle budget — the same drain budget a retire uses.

    A recycled worker's replacement gets the app's declared drain window to boot and rejoin the census.
    """
    from tai42_skeleton.app import instance

    return instance.build_app().drain_budgets.budget()


NO_REPORT_ROW = "no reload report row"
"""Why a worker with no row in the reload report has not stated its env."""

UNREACHABLE_REPORT = "the reload broadcast did not reach the bus"
"""Why a worker with no row in a reload report the bus could not complete has not stated its env."""

MALFORMED_REPLY = "its reload reply carries no awaiting_respawn list"
"""Why a worker whose reload reply — ``applied``, or ``failed`` stating a discarded build — lacks a well-formed
``awaiting_respawn`` list has not stated its env."""

REFUSED_BUILD = (
    "its reload was refused at its epoch build: the stored config did not build on it, and a restart may fail the same "
    "way, so it keeps serving its current config"
)
"""Why a roll makes no restart for the keys a worker reports whose reload was refused at its epoch build.

Refused means the build was discarded before its swap and the reply states it (``failed`` with
``{status: "discarded", awaiting_respawn}``); a failed discard is no refused build, and leaves the
worker's state unknown.
"""


@dataclass(frozen=True)
class RecycleReason:
    """Why one worker needs a restart: the recycle-class ``keys`` it reported, or what left its state ``unknown``.

    ``keys`` are the names a worker's reload reply states as moved off the value it bound them
    under; ``unknown`` names what kept the worker from stating it (its reload outcome, no row, a
    malformed reply, an unreachable bus), and is ``None`` for a worker that reported keys.
    ``withheld`` is why the roll makes no restart for the keys reported (:data:`REFUSED_BUILD`:
    the worker's reload was refused at its epoch build), ``None`` for a restart the roll makes.
    Names only — never values.
    """

    keys: tuple[str, ...] = ()
    unknown: str | None = None
    withheld: str | None = None


@dataclass(frozen=True)
class ReportedKeys:
    """The recycle-class ``keys`` a worker's reload reported, and the ``generation`` of the life that stated them."""

    generation: int
    keys: tuple[str, ...]


@dataclass(frozen=True)
class RecycleDecision:
    """Whom a fleet recycle restarts, whom it asks first, and whom it leaves running, read from one reload report.

    ``reported`` maps every worker whose reload stated recycle-class keys off their bound value to
    those keys and the generation of the life that stated them: the roll restarts that life without
    asking again. ``unconfirmed`` maps every worker other than the writer whose state the report
    leaves unknown to what kept it from stating its env: the roll waits it to ready and asks it with
    a targeted reload, and its reply decides (:func:`judge_row`). ``clean`` are the workers that
    confirmed the reload with no recycle-class key off its bound value, left running. ``withheld``
    maps every worker whose reload was refused at its epoch build yet reports recycle-class keys off
    their bound value to those keys: the stored config did not build on it and a restart may fail
    the same way, so it is left running on its current config, never asked, and named in the verdict
    with its keys. ``addressed`` names the workers a reload restricted to named targets reached
    (``None``: the whole fleet); the roll visits no other worker. ``writer`` is the bus member the
    change was written for; ``writer_reason`` is why it needs its own restart, ``None`` when it
    confirmed clean or the reload did not address it — the writer cannot be asked at its own roll,
    so its row is its statement; a reason whose restart is ``withheld`` is one the roll does not
    make. ``report_unreachable`` is whether the report is the bus-unreachable shape: no worker
    outside its rows stated its env, so the roll runs and asks every worker on its own census that
    the decision did not judge — of ``addressed`` only, when the reload named its targets.
    """

    writer: WorkerIdentity
    reported: Mapping[str, ReportedKeys]
    unconfirmed: Mapping[str, str]
    clean: frozenset[str]
    writer_reason: RecycleReason | None
    withheld: Mapping[str, tuple[str, ...]]
    addressed: frozenset[str] | None
    report_unreachable: bool

    @property
    def writer_needs(self) -> bool:
        """Whether the writer needs a restart the roll makes: it reported keys not withheld, or its state is unknown."""
        return self.writer_reason is not None and self.writer_reason.withheld is None

    def rolls_over(self, name: str) -> bool:
        """Whether the roll visits ``name`` — restarts it, or asks it first.

        Not the writer, not clean, not withheld, and addressed when the reload named its targets.
        """
        return (
            name != self.writer.name
            and name not in self.clean
            and name not in self.withheld
            and (self.addressed is None or name in self.addressed)
        )

    def why_unconfirmed(self, name: str) -> str:
        """What kept ``name``'s life now on the roll from stating its env, recorded when the roll asks it.

        Its decided reason; for a worker that reported keys, that the life which reported them is
        not the one now ready; for a worker the decision never saw, that the reload broadcast did
        not reach the bus when the report is the bus-unreachable shape, else no report row (it
        joined after the change began).
        """
        reported = self.reported.get(name)
        if reported is not None:
            return (
                f"its reload reported {', '.join(reported.keys)} as generation {reported.generation}, "
                "a life no longer on the census"
            )
        return self.unconfirmed.get(name, UNREACHABLE_REPORT if self.report_unreachable else NO_REPORT_ROW)


def recycle_decision(
    report: FleetResult,
    pinned: Mapping[str, tuple[WorkerKind, int]],
    writer: WorkerIdentity,
    *,
    addressed: Set[str] | None = None,
) -> RecycleDecision:
    """Decide from the change's reload ``report`` whom a fleet recycle restarts, asks first, or leaves running.

    Every worker in ``pinned`` (the census read before the reload, the writer excluded) or with a
    row in ``report`` is exactly one of: confirmed clean — its row is ``applied``, or ``failed``
    with a payload stating its epoch build was discarded before the swap, and its payload's
    ``awaiting_respawn`` is an empty list (:func:`judge_row`); withheld — its row is such a
    ``failed`` one listing keys, pinned or not: the stored config did not build on it and a restart
    may fail the same way, so the roll leaves it running and the verdict names it; reported — its
    ``applied`` row lists keys and its name is pinned, which ties the statement to the pinned life;
    or unconfirmed — any other outcome, a pinned worker with no row, a payload without a well-formed
    ``awaiting_respawn`` list (logged at ERROR naming the worker: a reload reply without it breaks
    the reload contract), an unreachable report with no row for it, or keys from a worker joined too
    late to be pinned. An unconfirmed worker is never read as clean: a sibling is asked once it is
    ready, and the roll restarts it unless its reply confirms it clean or states a refused
    build, its restart then withheld. A row present in a report the bus could not complete (a
    worker's own row its decision keeps, :func:`with_own_statement`: a joiner's to its failed
    announcement, a writer's to its reload's broadcast) is still that worker's statement, judged as
    any other row. Such a report states no other worker's env, and ``pinned`` may not name them all
    (an op-start census that could not be read pins nothing): the decision marks it
    ``report_unreachable``, so the roll runs and asks every worker on its own census that the
    decision did not judge — of ``addressed`` only, when the reload named its targets. The writer is
    judged by the same rule from its own row, and is never asked at its own roll: when its own state
    is unknown, it needs its own restart.

    ``addressed`` names the workers a reload restricted to named targets reached (``None``: the
    whole fleet). Only they are judged; every other worker is left out of the decision and the
    roll, and a writer outside them is not judged either.
    """
    rows = {row.name: row for row in report.results}
    names = (set(pinned) | set(rows)) - {writer.name}
    if addressed is not None:
        names &= set(addressed)
    reported: dict[str, ReportedKeys] = {}
    unconfirmed: dict[str, str] = {}
    withheld: dict[str, tuple[str, ...]] = {}
    clean: set[str] = set()
    for name in sorted(names):
        reason = _reason(rows.get(name), report_reachable=report.reachable)
        if reason is None:
            clean.add(name)
        elif reason.withheld is not None:
            withheld[name] = reason.keys
        elif reason.unknown is not None:
            unconfirmed[name] = reason.unknown
        elif name in pinned:
            reported[name] = ReportedKeys(generation=pinned[name][1], keys=reason.keys)
        else:
            unconfirmed[name] = f"its reload reported {', '.join(reason.keys)} from a life the change did not pin"
    return RecycleDecision(
        writer=writer,
        reported=reported,
        unconfirmed=unconfirmed,
        clean=frozenset(clean),
        withheld=withheld,
        writer_reason=(
            None
            if addressed is not None and writer.name not in addressed
            else _reason(rows.get(writer.name), report_reachable=report.reachable)
        ),
        addressed=None if addressed is None else frozenset(addressed),
        report_unreachable=not report.reachable,
    )


def _reason(row: WorkerResult | None, *, report_reachable: bool) -> RecycleReason | None:
    """Why the worker behind ``row`` needs a restart, ``None`` when its row confirms it clean.

    A worker with no row has not stated its env: the bus could not be read, or no row came back.
    """
    if row is None:
        return RecycleReason(unknown=NO_REPORT_ROW if report_reachable else UNREACHABLE_REPORT)
    return judge_row(row)


def answers_for_env(row: WorkerResult) -> bool:
    """Whether ``row``, a worker's reload row, is a reply that answers for that worker's env.

    Two rows do: an ``applied`` row, and a ``failed`` row whose payload states its epoch build was
    discarded before the swap (:func:`~tai42_skeleton.app.discarded_build.states_discarded_build`),
    the worker serving its previous epoch under the env the discard restored. Such a row's answer
    is its ``awaiting_respawn`` list, which :func:`judge_row` reads: a row without a well-formed
    list answers but states nothing (:data:`MALFORMED_REPLY`). Any other row does not answer, and
    leaves the worker's env unknown.
    """
    if row.outcome is OpOutcome.applied:
        return True
    return row.outcome is OpOutcome.failed and states_discarded_build(row.payload)


def with_own_statement(report: FleetResult, own: WorkerResult) -> FleetResult:
    """``report`` with ``own``, a worker's own reload row, in place of any row of that worker.

    A worker knows what its own reload stated even when the bus could not complete ``report``, so
    the decision reads that row when it answers for the worker's env (:func:`answers_for_env`), and
    judges it as any other row. Any other row of its own leaves ``report`` as it is.
    """
    if not answers_for_env(own):
        return report
    return report.model_copy(update={"results": [own, *(row for row in report.results if row.name != own.name)]})


def judge_row(row: WorkerResult) -> RecycleReason | None:
    """What one worker's reload row states of its env: ``None`` when clean, else the reason it needs a restart.

    The one judgment of a reload row, read alike from the change's report and from the reply of a
    worker the roll asks once it is ready. Two rows answer for the worker's env
    (:func:`answers_for_env`): an ``applied`` row, and a ``failed`` row stating its epoch build was
    discarded before the swap.
    Both are read by one list rule: an empty ``awaiting_respawn`` list is clean, one listing keys
    reports them, and a payload without a well-formed list (logged at ERROR naming the worker)
    leaves its state unknown. Keys a discarded build reports carry the restart
    :data:`REFUSED_BUILD` withholds: the stored config did not build on that worker, and a restart
    may fail the same way. Any other ``failed`` row, and any other outcome, leaves its state
    unknown.
    """
    if not answers_for_env(row):
        return RecycleReason(unknown=f"its reload reported {row.outcome.value}")
    discarded = row.outcome is OpOutcome.failed
    keys = row.payload.get("awaiting_respawn") if isinstance(row.payload, Mapping) else None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        logger.error(
            "worker %r answered the reload with no well-formed awaiting_respawn list in its reply — its env is "
            "taken as unknown",
            row.name,
        )
        return RecycleReason(unknown=MALFORMED_REPLY)
    if not keys:
        return None
    return RecycleReason(keys=tuple(keys), withheld=REFUSED_BUILD if discarded else None)
