"""One attempt of a hook-run job: run the hook (or replay its start) under a lease, then record the verdict.

An attempt is a root task spawned by :func:`~tai42_skeleton.hooks.runs.runtime.spawn_attempt`. It
inherits nothing from its spawner and binds every fact it acts under from the job. Every store step
reads the current hooks manager at that moment and is fenced on the claimer id the job carries, so
an attempt begun before a config apply finishes on the fresh manager.

Whether the hook has acted is the key to every verdict. The hook's visit reports it through its
``on_act`` notice at the real action point: before a cancel or a start, and before and after each
resume or take claim. The attempt keeps that state in memory and writes it with each verdict, so a
verdict never depends on a marker write that failed.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager, AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Concatenate, Literal

from tai42_contract.app import tai42_app
from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.interactions import ParkableRunFailedError, ParkedEntryGoneError, VisitAct
from tai42_contract.monitoring import SpanKind

from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.monitoring import get_monitoring
from tai42_skeleton.operations.errors import NotFoundError, PermissionDeniedError
from tai42_skeleton.states.context import state_context

from . import runtime
from .loud import report_failed
from .metrics import hook_run_metrics
from .models import ClaimIntent, HookRun, HookRunActed, StartSpec

logger = logging.getLogger(__name__)

# Failure kinds no retry can change: a run that fails with one is held as failed at once.
TERMINAL_KINDS: frozenset[str] = frozenset(
    {
        ErrorKind.BAD_INPUT,
        ErrorKind.UNAUTHORIZED,
        ErrorKind.NOT_FOUND,
        ErrorKind.CONFLICT,
        ErrorKind.CANCELLED,
    }
)

Disposition = Literal["rerun", "replay_start", "fail"]


class HookRunLeaseLostError(RuntimeError):
    """The attempt's fenced write found the job no longer claimed by this attempt's claimer."""


@dataclass(frozen=True, slots=True)
class _Verdict:
    disposition: Disposition
    error: str = ""


async def _disposition(acted: HookRunActed, start: StartSpec | None, *, attempts_used: int) -> _Verdict:
    from tai42_skeleton.operations.tool_runs.reconcile import _tool_declares_crash_resume

    max_attempts = runtime.manager().settings.run_max_attempts
    if acted == "":
        disposition: Disposition = "rerun"
    elif acted == "start" and start is not None and await _tool_declares_crash_resume(start.tool):
        disposition = "replay_start"
    else:
        return _Verdict("fail", f"interrupted after the hook acted ({acted})")
    if attempts_used > max_attempts:
        return _Verdict(
            "fail",
            f"interrupted on attempt {attempts_used - 1} of {max_attempts} and no attempts remain (acted={acted})",
        )
    return _Verdict(disposition)


async def interrupted_disposition(job: HookRun, *, attempts_used: int) -> Disposition:
    """What a job freed with no verdict does next: run the hook again, replay its start alone, or fail.

    ``attempts_used`` is the count the next run of the job would use: ``job.attempts`` on a reclaim
    (the claim already counted it), ``job.attempts + 1`` on a release (the next claim counts it).
    A run whose hook has not acted runs again; a start of a tool that declares crash-resume is
    replayed alone; either fails once the attempts are used up. Every other acted run fails.
    """
    return (await _disposition(job.acted, job.start, attempts_used=attempts_used)).disposition


def failure_text(exc: BaseException) -> str:
    """The ``last_error`` an exception records; a failed take keeps the stored failure it carried."""
    if isinstance(exc, ParkableRunFailedError):
        return f"{exc}: {exc.error!r}"
    return f"{type(exc).__name__}: {exc}"


def untold_claim_text(
    run_id: str,
    intent: ClaimIntent,
    gone: tuple[str, ...],
    claim_error: str | None,
    recorded_error: str | None = None,
) -> str:
    """The failure text of a claim whose entry is no longer open, so the store cannot tell who resolved it.

    ``claim_error`` is this attempt's own claim-call failure. ``recorded_error`` is the run's last
    recorded error when the intent is settled at the start of an attempt; it carries the claim
    call's failure an earlier attempt recorded when its own settle failed.
    """
    text = (
        f"hook run {run_id}: its hook was making a {intent.action} claim on {list(intent.ids)} when the run "
        f"stopped before confirming it. These are no longer open: {list(gone)}. The interactions store cannot "
        "tell whether this run's claim resolved them or another door did (an answer, a reaper expiry, another "
        "caller, the retention sweep or a TTL). Nothing was re-run. Check the interactions, then discard this "
        "run, or retry it: a retry is a fresh run whose checks and door contract decide again, and it may act "
        "again if the claim was not this run's."
    )
    if claim_error is not None:
        text += f" The claim call failed with: {claim_error}"
    if recorded_error is not None:
        text += f" The run's last recorded error: {recorded_error}"
    return text


def _runs_to_its_end[**P, R](
    step: Callable[Concatenate[_Attempt, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[_Attempt, P], Coroutine[Any, Any, R]]:
    """Make a verdict step run to its end when the attempt is cancelled while it runs.

    The step runs as its own task, so a cancel of the attempt never stops a verdict write or
    report half way. The cancel is held until the step has ended, then raised, and the attempt
    notes that its verdict was recorded so the cancel frees nothing further. A second cancel
    cancels the step itself. A step that raised meanwhile is logged at ERROR before the cancel
    is raised.
    """

    @functools.wraps(step)
    async def _step(attempt: _Attempt, *args: P.args, **kwargs: P.kwargs) -> R:
        task = asyncio.create_task(step(attempt, *args, **kwargs))
        cancel: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError as exc:
                if cancel is not None:
                    task.cancel()
                cancel = exc
        if cancel is None:
            return task.result()
        attempt.verdict_recorded = True
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error(
                "hook run %s: %s raised while a cancel waited for it", attempt.job.id, step.__name__, exc_info=error
            )
        raise cancel

    return _step


async def run_attempt(job: HookRun) -> None:
    """Run one attempt of ``job``, claimed by this process, inside the job's monitoring scope."""
    scope: AbstractContextManager[Any] = get_monitoring().writer.disable() if job.monitoring_disabled else nullcontext()
    with scope:
        await _Attempt(job).run()


class _Attempt:
    """The state of one attempt: what the hook has acted on so far, and the claim it has yet to confirm."""

    def __init__(self, job: HookRun) -> None:
        if job.claimed_by is None:
            raise ValueError(f"hook run {job.id} was handed to an attempt without a claimer")
        self.job = job
        self.claimed_by: str = job.claimed_by
        self.acted: HookRunActed = job.acted
        self.acted_ids: list[str] = list(job.acted_ids)
        self.intent: ClaimIntent | None = job.claim_intent
        self.start: StartSpec | None = job.start
        self.verdict_recorded = False

    # -- the attempt ------------------------------------------------------------

    async def run(self) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(), name=f"tai-hook-run-heartbeat-{self.job.id}")
        slot = False
        try:
            try:
                await runtime.acquire_slot()
                slot = True
                await self._drive_in_span()
            except HookRunLeaseLostError:
                logger.warning(
                    "hook run %s lost its lease during attempt %d; this attempt stops and records nothing",
                    self.job.id,
                    self.job.attempts,
                )
            except asyncio.CancelledError as exc:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    if not self.verdict_recorded:
                        try:
                            await self._interrupt("shutdown")
                        except Exception:
                            logger.exception(
                                "hook run %s: it could not be freed after the shutdown cancel; the lease reclaims it",
                                self.job.id,
                            )
                    raise
                from tai42_skeleton.operations.tool_runs.supervisor import ToolRunDrainedError

                if isinstance(exc, ToolRunDrainedError):
                    await self._interrupt("drain", reason=exc.reason)
                    return
                hook_run_metrics().attempt_failures.labels(error_kind=ErrorKind.CANCELLED.value).inc()
                await self._fail(
                    "the hook's work raised its own CancelledError; no drain and no stop cancelled this "
                    f"attempt (acted={self.acted})",
                    ErrorKind.CANCELLED.value,
                )
            except Exception as exc:
                await self._on_failure(exc)
        finally:
            heartbeat.cancel()
            if slot:
                runtime.release_slot()

    async def _drive_in_span(self) -> None:
        writer = get_monitoring().writer
        job = self.job
        with (
            writer.start_span(name="hook_run_attempt", kind=SpanKind.CHAIN),
            writer.trace_attributes(
                tags=[f"hook:{job.topic}"], metadata={"topic": job.topic}, name=f"event: {job.topic}"
            ),
        ):
            writer.update_current_span(
                metadata={
                    "topic": job.topic,
                    "hook": job.hook,
                    "run_id": job.id,
                    "attempt": job.attempts,
                    "mode": "replay_start" if job.acted == "start" else "hook",
                }
            )
            await self._drive()

    async def _drive(self) -> None:
        job = self.job
        if (
            self.intent is not None
            and await self._settle(self.intent, claim_error=None, recorded_error=job.last_error) != "open"
        ):
            return
        if job.reclaimed:
            hook_run_metrics().interrupted.labels(cause="lease").inc()
            verdict = await _disposition(self.acted, self.start, attempts_used=job.attempts)
            if verdict.disposition == "fail":
                logger.warning("hook run %s: its lease lapsed and it cannot go on: %s", job.id, verdict.error)
                await self._fail(verdict.error, ErrorKind.UNKNOWN.value)
                return
            logger.warning(
                "hook run %s: its lease lapsed; attempt %d goes on (%s)", job.id, job.attempts, verdict.disposition
            )
        hook = (await runtime.manager().list_hooks_by_topic(job.topic)).get(job.hook)
        if hook is None:
            raise NotFoundError(f"hook {job.hook!r} on topic {job.topic!r} is no longer registered")
        link_scope: AbstractAsyncContextManager[Any] = (
            bind_execution_identity(job.dispatch_key.execution_key, bound_fingerprint=job.dispatch_key.fingerprint)
            if job.dispatch_key is not None
            else nullcontext()
        )
        async with link_scope:
            if self.acted == "start":
                await self._replay_start(hook)
            else:
                mgr = runtime.manager()
                if not await mgr._check_condition(hook, job.payload):
                    await self._finish()
                    return
                await runtime.manager()._run_hook(
                    hook,
                    job.payload,
                    job.tool_kwargs_override,
                    idempotency_key=job.id,
                    mark_acted=self._mark,
                )
        await self._finish()

    async def _replay_start(self, hook: Any) -> None:
        from tai42_skeleton.hooks.managers.base_hooks_manager import recorded_start
        from tai42_skeleton.operations.tool_runs import run_recorded

        start = self.start
        if start is None:
            raise ValueError(f"hook run {self.job.id} records a start but carries no start spec to replay")
        if not hook.execution_key:
            raise PermissionDeniedError(f"hook {hook.name!r} binds no execution key; refusing to fire")
        context_scope: AbstractContextManager[Any] = (
            state_context(start.state_context) if start.state_context is not None else nullcontext()
        )

        async def _on_act(act: VisitAct) -> None:
            await self._mark(act, start)

        async with bind_execution_identity(hook.execution_key, bound_fingerprint=hook.execution_key_fingerprint):
            with context_scope:
                await tai42_app.interactions.visit(
                    target_name=start.tool,
                    cancel=[],
                    resume=[],
                    start=recorded_start(start.tool, start.input, run_recorded, self.job.id),
                    extras=start.extras,
                    state_binding=start.state_binding,
                    receives_outcome=False,
                    on_act=_on_act,
                )

    # -- the marker -------------------------------------------------------------

    async def _mark(self, act: VisitAct, start_spec: StartSpec | None) -> None:
        """Record a visit notice: in memory, and as a fenced write on the job.

        A ``before`` notice is written first and held in memory only once written, so a failed write
        stops the visit before that action and leaves the attempt's state as it was. A ``committed``
        notice is held in memory first, because the claim already stands whatever the write does.
        """
        job = self.job
        mgr = runtime.manager()
        if act.phase == "committed":
            self.acted = act.action
            self.acted_ids.extend(act.interaction_ids)
            self.intent = None
            if not await mgr._mark_acted(job.id, self.claimed_by, act, None):
                raise HookRunLeaseLostError(job.id)
            return
        if not await mgr._mark_acted(job.id, self.claimed_by, act, start_spec):
            raise HookRunLeaseLostError(job.id)
        if act.action in ("resume", "take"):
            self.intent = ClaimIntent(action=act.action, ids=act.interaction_ids)
            return
        self.acted = act.action
        self.acted_ids.extend(act.interaction_ids)
        if start_spec is not None:
            self.start = start_spec

    # -- the settle -------------------------------------------------------------

    async def _settle(
        self, intent: ClaimIntent, *, claim_error: str | None, recorded_error: str | None = None
    ) -> Literal["open", "stopped"]:
        """Settle the unconfirmed claim intent: clear it when every entry is still open, else stop the job.

        An entry no longer open cannot be told apart from another door's resolution, so the job is
        held as failed with that text. A settle that raises never guesses: a job whose hook has not
        acted is rescheduled with its intent kept, any other is failed with it kept. When the claim
        call itself failed, that error is logged and recorded beside the settle's own.
        """
        job = self.job
        metrics = hook_run_metrics()
        try:
            gone = await tai42_app.interactions.settle_visit_claim(VisitAct(intent.action, intent.ids, "before"))
        except Exception as exc:
            metrics.claim_settles.labels(result="error").inc()
            logger.exception(
                "hook run %s: the %s claim on %s could not be settled%s",
                job.id,
                intent.action,
                list(intent.ids),
                "" if claim_error is None else f"; the claim call itself failed with: {claim_error}",
            )
            text = None
            if claim_error is not None:
                text = (
                    f"the {intent.action} claim call failed with: {claim_error}; "
                    f"settling the claim then failed with: {failure_text(exc)}"
                )
            await self._classify(exc, text=text)
            return "stopped"
        if not gone:
            metrics.claim_settles.labels(result="open").inc()
            if not await runtime.manager()._clear_intent(job.id, self.claimed_by):
                raise HookRunLeaseLostError(job.id)
            self.intent = None
            return "open"
        metrics.claim_settles.labels(result="untold").inc()
        await self._fail(
            untold_claim_text(job.id, intent, tuple(gone), claim_error, recorded_error), ErrorKind.CONFLICT.value
        )
        return "stopped"

    # -- the verdicts -----------------------------------------------------------

    async def _on_failure(self, exc: Exception) -> None:
        job = self.job
        if self.intent is not None:
            if isinstance(exc, ParkedEntryGoneError):
                # The claim call answered a definite no: it lost its race and did not commit.
                try:
                    if not await runtime.manager()._clear_intent(job.id, self.claimed_by):
                        return
                except Exception:
                    logger.exception("hook run %s: its lost claim intent could not be cleared", job.id)
                    return
                self.intent = None
            else:
                try:
                    settled = await self._settle(self.intent, claim_error=failure_text(exc))
                except HookRunLeaseLostError:
                    return
                if settled != "open":
                    return
        await self._classify(exc)

    async def _classify(self, exc: BaseException, *, text: str | None = None) -> None:
        """Retry a failure before the hook acted with backoff; hold every other one as failed.

        ``exc`` decides the kind; ``text`` replaces its failure text as the recorded error.
        """
        job = self.job
        kind = error_kind(exc).value
        if text is None:
            text = failure_text(exc)
        hook_run_metrics().attempt_failures.labels(error_kind=kind).inc()
        settings = runtime.manager().settings
        if self.acted != "" or kind in TERMINAL_KINDS or job.attempts >= settings.run_max_attempts:
            await self._fail(text, kind)
            return
        delay = min(settings.run_retry_cap_seconds, settings.run_retry_base_seconds * 2 ** (job.attempts - 1))
        logger.warning(
            "hook run %s (%s on %s) attempt %d of %d failed [%s]; retrying in %.1fs: %s",
            job.id,
            job.hook,
            job.topic,
            job.attempts,
            settings.run_max_attempts,
            kind,
            delay,
            text,
        )
        try:
            await runtime.manager()._reschedule_run(job.id, self.claimed_by, runtime.now() + delay, text, kind)
        except Exception:
            logger.exception("hook run %s: its retry could not be scheduled; the lease reclaims it", job.id)

    async def _interrupt(self, cause: Literal["shutdown", "drain"], *, reason: str | None = None) -> None:
        """Free a job interrupted with no verdict: release it for the next attempt, or fail it by the rule."""
        job = self.job
        hook_run_metrics().interrupted.labels(cause=cause).inc()
        if reason is not None:
            logger.warning("hook run %s: a drain interrupted its tool: %s", job.id, reason)
        try:
            if self.intent is not None and await self._settle(self.intent, claim_error=None) != "open":
                return
        except HookRunLeaseLostError:
            return
        verdict = await _disposition(self.acted, self.start, attempts_used=job.attempts + 1)
        if verdict.disposition == "fail":
            error = verdict.error if reason is None else f"{verdict.error}: {reason}"
            logger.warning("hook run %s: interrupted (%s) and failed: %s", job.id, cause, error)
            await self._fail(error, ErrorKind.UNKNOWN.value)
            return
        logger.warning(
            "hook run %s: interrupted (%s); released for its next attempt (%s)", job.id, cause, verdict.disposition
        )
        try:
            await runtime.manager()._release_run(job.id, self.claimed_by, runtime.now())
        except Exception:
            logger.exception("hook run %s: it could not be released; the lease reclaims it", job.id)

    @_runs_to_its_end
    async def _finish(self) -> None:
        try:
            finished = await runtime.manager()._finish_run(self.job.id, self.claimed_by)
        except Exception:
            logger.exception("hook run %s: its success could not be recorded; the lease reclaims it", self.job.id)
            return
        if finished:
            hook_run_metrics().succeeded.inc()

    @_runs_to_its_end
    async def _fail(self, error: str, kind: str) -> None:
        job = self.job
        try:
            run = await runtime.manager()._fail_run(
                job.id,
                self.claimed_by,
                error,
                kind,
                runtime.now(),
                self.acted,
                tuple(self.acted_ids),
                self.intent,
            )
        except Exception:
            logger.exception(
                "hook run %s: its failure could not be recorded (%s); the lease reclaims it", job.id, error
            )
            return
        if run is not None:
            await report_failed(run)

    # -- the lease --------------------------------------------------------------

    async def _heartbeat(self) -> None:
        job = self.job
        while True:
            lease = runtime.manager().settings.run_lease_seconds
            await asyncio.sleep(lease / 3)
            try:
                renewed = await runtime.manager()._renew_lease(job.id, self.claimed_by, runtime.now() + lease)
            except Exception:
                logger.warning("hook run %s: its lease could not be renewed; trying again", job.id, exc_info=True)
                continue
            if not renewed:
                logger.warning("hook run %s: its lease was lost; the heartbeat stops", job.id)
                return


__all__ = [
    "TERMINAL_KINDS",
    "HookRunLeaseLostError",
    "failure_text",
    "interrupted_disposition",
    "run_attempt",
    "untold_claim_text",
]
