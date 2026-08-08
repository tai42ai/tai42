"""Recycle orchestration — the rolling per-kind recycle loop.

Called from the profile-apply pipeline when the diff carries recycle-class
keys on a supervised shape. It recycles the fleet ONE origin at a time per kind,
waiting for a genuinely NEW origin of that kind to rejoin the census before moving on,
so the fleet never loses every worker of a kind at once and every replacement has
loaded the new env (census presence is registered only AFTER boot resync).

The applying origin is EXCLUDED (a caller parameter): the bus echo-skips the
publisher's own frames, so the applier can never handle its own recycle op — targeting
it would time out spuriously. When the diff carries serve-affecting recycle keys the
applier's OWN recycle is a deferred post-response self-exit it cannot confirm, reported
as an ``applier`` entry with the :data:`SELF_DEFERRED` status — never as a confirmed
replacement.

The report enumerates origin NAMES only, never env values (the same names-only
property the apply response carries).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, Field

from tai42_skeleton.app.bus import OpOutcome, WorkerBus, WorkerKind

SELF_DEFERRED = "self-deferred"
"""The applier self-entry status in a recycle report: the applying serve worker's own
recycle is a deferred post-response self-exit it cannot confirm. One shared literal,
so the applier self-entry status never diverges across its readers."""

# Recycle one KIND at a time in this order: a backend replacement must be serving
# before serve workers roll, so no kind loses every worker at once.
_KIND_ORDER: tuple[WorkerKind, ...] = (WorkerKind.backend, WorkerKind.serve)


class ApplierEntry(BaseModel):
    """The applier's own deferred self-exit entry — present only when the diff carries
    serve-affecting recycle keys."""

    origin: str
    status: str = SELF_DEFERRED


class RecycleReport(BaseModel):
    """The aggregate outcome of a recycle orchestration. Names only — ``recycled`` and
    ``timeouts`` are the targeted origins, ``replacements`` the new origins that
    rejoined; ``applier`` (when set) is the deferred self-exit."""

    recycled: list[str] = Field(default_factory=list)
    replacements: list[str] = Field(default_factory=list)
    timeouts: list[str] = Field(default_factory=list)
    applier: ApplierEntry | None = None


class RecycleError(RuntimeError):
    """A recycle step failed loudly; carries the partial :class:`RecycleReport` so the
    caller can surface what converged before the failure."""

    def __init__(self, message: str, report: RecycleReport) -> None:
        super().__init__(message)
        self.report = report


class RecycleTimeoutError(RecycleError):
    """A recycled origin's replacement never rejoined the census within the step
    budget. Names the origin loudly; the whole apply aborts."""

    def __init__(self, origin: str, report: RecycleReport) -> None:
        super().__init__(
            f"recycle: a replacement for {origin!r} never joined the worker-bus census within the step budget",
            report,
        )
        self.origin = origin


async def orchestrate_recycle(
    bus: WorkerBus,
    *,
    excluded_origin: str,
    target_kinds: Sequence[WorkerKind],
    applier_self_deferred: bool,
    step_timeout: float,
    poll_interval: float = 0.2,
) -> RecycleReport:
    """Roll a recycle across the fleet, kind by kind, excluding ``excluded_origin``.

    For each targeted kind, snapshot the origins to recycle (excluding the applier),
    then recycle them one at a time: publish the recycle op to the single target, await
    its ``applied`` terminal, and wait for a genuinely NEW origin of that kind to join
    the census. A recycle op that does not apply, or a replacement that never joins
    within ``step_timeout``, raises loudly (:class:`RecycleError` /
    :class:`RecycleTimeoutError`) carrying the partial report.
    """
    report = RecycleReport()
    if applier_self_deferred:
        report.applier = ApplierEntry(origin=excluded_origin)

    wanted = set(target_kinds)
    for kind in _KIND_ORDER:
        if kind not in wanted:
            continue
        targets = [o.name for o in await bus.census() if o.kind is kind and o.name != excluded_origin]
        for target in targets:
            before = {o.name for o in await bus.census() if o.kind is kind}
            await _recycle_one(bus, target, report)
            report.recycled.append(target)
            replacement = await _await_replacement(bus, kind, before, target, step_timeout, poll_interval, report)
            report.replacements.append(replacement)
    return report


async def _recycle_one(bus: WorkerBus, target: str, report: RecycleReport) -> None:
    """Publish the recycle op to a single target and require its ``applied`` terminal.
    A bus-unreachable publish or a non-applied outcome raises loudly."""
    result = await bus.publish({"op": "recycle"}, targets=[target], local=None)
    if not result.reachable:
        raise RecycleError(f"recycle: bus unreachable while recycling {target!r}: {result.error}", report)
    entry = next((r for r in result.results if r.name == target), None)
    if entry is None or entry.outcome is not OpOutcome.applied:
        detail = (entry.error or entry.detail) if entry is not None else "no reply from the target"
        raise RecycleError(f"recycle: worker {target!r} did not apply the recycle op ({detail})", report)


async def _await_replacement(
    bus: WorkerBus,
    kind: WorkerKind,
    before: set[str],
    target: str,
    step_timeout: float,
    poll_interval: float,
    report: RecycleReport,
) -> str:
    """Poll the census until a NEW origin of ``kind`` (absent from ``before``) appears,
    returning its name. On timeout, record ``target`` in the report and raise loudly —
    census presence is registered only after boot resync, so a new origin means the
    replacement has loaded the new env."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + step_timeout
    while True:
        fresh = sorted(o.name for o in await bus.census() if o.kind is kind and o.name not in before)
        if fresh:
            return fresh[0]
        if loop.time() >= deadline:
            report.timeouts.append(target)
            raise RecycleTimeoutError(target, report)
        await asyncio.sleep(poll_interval)
