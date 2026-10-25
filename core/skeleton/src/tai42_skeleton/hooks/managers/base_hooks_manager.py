"""The abstract hooks-manager base: hook registration, the hook-run store, and the hook fire."""

from __future__ import annotations

import logging
import secrets
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from tai42_kit.interactions import DoorContractOutcome

from tai42_contract.app import tai42_app
from tai42_contract.hooks.models import HookParams, HookSubject, TopicVerifierBinding
from tai42_contract.interactions import VisitAct
from tai42_contract.interactions.door_contract import PARKED_VARIABLE
from tai42_contract.monitoring import MonitoringLevel, SpanKind
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_contract.template import TemplatedText
from tai42_contract.tools import TOOL_EXTRA_IDEMPOTENCY_KEY
from tai42_kit.interactions import evaluate_door_contract, parked_entries_for_jq
from tai42_kit.utils.data import run_jq_first
from tai42_kit.utils.data.jq_util import compile_check, get_compiled_jq

from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.hooks.runs.models import (
    AcceptResult,
    ClaimIntent,
    DispatchKey,
    HookRun,
    HookRunActed,
    HookRunCounts,
    HookRunsPage,
    ReplayClaim,
    RetryResult,
    StartSpec,
)
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.monitoring import get_monitoring
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.states.context import state_context
from tai42_skeleton.template.references import assert_templates_stored
from tai42_skeleton.template.stored_refs import hook_template_refs

logger = logging.getLogger(__name__)


class BaseHooksManager(ABC):
    """Base for hook managers: registration, per-topic verifier bindings, and the hook-run store.

    Backends supply the storage of hooks, verifier bindings, the replay seen-set and the hook-run
    jobs; this base supplies the accept, the condition check and the hook fire every attempt runs.
    """

    def __init__(self, settings: HooksSettings):
        """Store ``settings``."""
        self.settings = settings

    @staticmethod
    def validate_jq_fields(params: HookParams) -> None:
        """Reject inline jq that does not compile, at registration time.

        A broken condition or door-contract expression would otherwise surface only as a hook
        that never fires (indistinguishable from a false condition). A templated text naming a
        stored resource is checked to exist by ``register``; its content renders per event and is
        compiled then, so a compile failure surfaces loudly at fire time. The four door-contract
        expressions read the run's parked interactions as ``$parked``, so they are compiled with
        that variable declared.
        """
        condition: TemplatedText | None = params.condition
        if condition is not None and condition.content:
            try:
                get_compiled_jq(condition.content)
            except Exception as exc:
                raise ValueError(f"hook {params.name!r}: condition is not valid jq: {exc}") from exc
        for field in ("start_expr", "cancel_expr", "resume_expr", "extras_expr"):
            text: TemplatedText | None = getattr(params, field)
            if text is None or not text.content:
                continue
            try:
                compile_check(text.content, variables=(PARKED_VARIABLE,))
            except Exception as exc:
                raise ValueError(f"hook {params.name!r}: {field} is not valid jq: {exc}") from exc

    async def _check_condition(self, hook: HookParams, payload: dict[str, Any]) -> bool:
        writer = get_monitoring().writer
        with writer.start_span(name="hook_check_condition", kind=SpanKind.CHAIN):
            raw_condition = (
                await tai42_app.storage.resource_manager.render_templated_text(hook.condition)
                if hook.condition is not None
                else ""
            )
            writer.update_current_span(metadata={"hook_name": hook.name, "raw_condition": raw_condition})

            if not raw_condition:
                return True

            try:
                result = await run_jq_first(raw_condition, payload)
            except Exception as e:
                # A genuine jq EVALUATION error at fire time must surface loudly
                # (the registration-time validator's docstring promises this), not
                # be swallowed as a skipped hook -- a skip is indistinguishable
                # from a condition that cleanly evaluated to false. A condition
                # that evaluates without error to a falsy value still skips below.
                writer.update_current_span(level=MonitoringLevel.ERROR, status_message=str(e))
                raise
            return bool(result)

    @staticmethod
    async def _run_hook(
        hook: HookParams,
        payload: dict[str, Any],
        tool_kwargs_override: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
        mark_acted: Callable[[VisitAct, StartSpec | None], Awaitable[None]],
    ) -> None:
        """Fire one hook's tool AS the hook's bound execution key, reporting each action it takes.

        The bind stays HERE, inside the attempt's own task, so a hook is never fired under another
        hook's key or the server's unbounded authority. ``mark_acted`` hears every notice the visit
        gives before and after it acts; a start notice carries the :class:`StartSpec` the start
        runs with, so the start alone can be replayed. ``idempotency_key`` reaches the started tool
        as its ``TOOL_EXTRA_IDEMPOTENCY_KEY`` extra.
        """
        # Imported at call time: the operation module runs a module-level app-lifecycle
        # decorator, so a top-level import would force it to load before the app is bound.
        from tai42_skeleton.operations.tool_runs import run_recorded

        writer = get_monitoring().writer
        with writer.start_span(name="hook_run_tool", kind=SpanKind.CHAIN):
            writer.update_current_span(
                metadata={
                    "tool": hook.tool,
                    "tool_kwargs": hook.tool_kwargs,
                    "tool_kwargs_override": tool_kwargs_override,
                }
            )
            if not hook.execution_key:
                # No bound key means no authority to fire under; the server's own is not a
                # substitute. Refuse before any work.
                raise PermissionDeniedError(f"hook {hook.name!r} binds no execution key; refusing to fire")

            hook_context = await _hook_state_context(hook, payload)
            context_scope: AbstractContextManager[Any] = (
                state_context(hook_context) if hook_context is not None else nullcontext()
            )
            async with bind_execution_identity(hook.execution_key, bound_fingerprint=hook.execution_key_fingerprint):
                with context_scope:
                    # The door contract reads the run's own parked interactions as ``$parked``;
                    # fetch them once over the hook's own state context and feed them to the pure
                    # evaluator, so the cancel/resume/start/extras it yields are a function of the
                    # event payload and that injected list.
                    parked = await tai42_app.interactions.list_parked_for(hook_context)
                    outcome = await evaluate_door_contract(hook, payload, parked_entries_for_jq(parked))
                    tool_input = _hook_tool_input(hook, outcome, tool_kwargs_override)
                    start_spec = (
                        None
                        if tool_input is None
                        else StartSpec(
                            tool=hook.tool,
                            input=tool_input,
                            extras=dict(outcome.extras),
                            state_context=hook_context,
                            state_binding=hook.state_binding,
                        )
                    )

                    async def _on_act(act: VisitAct) -> None:
                        await mark_acted(act, start_spec if act.action == "start" else None)

                    # ``receives_outcome=False``: a hook is a receiver-less door — a started target
                    # that async-parks is subject-tracked, and a ``resume_expr`` resume of a
                    # ``to="caller"`` entry fires the run's own delivery rather than returning inline.
                    # ``visit`` deposits ``state_binding`` around ``start`` alone (never around a
                    # resume's continuation), so the hook keeps no ambient tool-invocation deposit.
                    await tai42_app.interactions.visit(
                        target_name=hook.tool,
                        cancel=outcome.cancel,
                        resume=outcome.resume,
                        start=(
                            None
                            if tool_input is None
                            else recorded_start(hook.tool, tool_input, run_recorded, idempotency_key)
                        ),
                        extras=outcome.extras,
                        state_binding=hook.state_binding,
                        receives_outcome=False,
                        on_act=_on_act,
                    )

    async def register(self, params: HookParams) -> bool:
        """Register the hook described by ``params``; return whether it was newly added.

        The one write service every hook write runs through (the register door and the backup
        restore alike): the stored template ids it names exist, the inline jq compiles, then a
        carried state binding is validated and its templates attached, then the backend stores the
        hook. A refusal raises before anything is stored.
        """
        await assert_templates_stored(hook_template_refs(params), ValueError)
        self.validate_jq_fields(params)
        if params.state_binding is not None:
            from tai42_skeleton.app import instance
            from tai42_skeleton.tools import state_binding

            await state_binding.validate_and_attach_binding(instance.app, params.state_binding)
        return await self._store_hook(params)

    @abstractmethod
    async def _store_hook(self, params: HookParams) -> bool:
        """Store the already-validated hook ``params``; return whether it was newly added."""
        ...

    @abstractmethod
    async def unregister(self, name: str) -> bool:
        """Remove the hook named ``name``; return whether one was removed."""
        ...

    @abstractmethod
    async def list_hooks(self) -> dict[str, HookParams]:
        """Return every registered hook, keyed by name."""
        ...

    @abstractmethod
    async def list_hooks_by_topic(self, topic: str) -> dict[str, HookParams]:
        """Return the hooks bound to ``topic``, keyed by name."""
        ...

    # -- Per-topic webhook-verifier bindings ---------------------------------
    #
    # A binding is the ``{"verifier": <name>, "config": {...}}`` shape validated
    # against ``TopicVerifierBinding`` naming a registered webhook verifier and its
    # per-topic config. The config carries a ``secret_env`` (an env var NAME),
    # never a secret value. Both manager backends validate on write and read, and the
    # reads return the model.

    @abstractmethod
    async def set_topic_verifier(self, topic: str, binding: dict[str, Any]) -> None:
        """Store the webhook-verifier ``binding`` for ``topic``."""
        ...

    @abstractmethod
    async def get_topic_verifier(self, topic: str) -> TopicVerifierBinding | None:
        """Return the webhook-verifier binding for ``topic``, or ``None`` when none is set."""
        ...

    @abstractmethod
    async def delete_topic_verifier(self, topic: str) -> bool:
        """Remove ``topic``'s webhook-verifier binding; return whether one was removed."""
        ...

    @abstractmethod
    async def all_topic_verifiers(self) -> dict[str, TopicVerifierBinding]:
        """Return every topic's webhook-verifier binding, keyed by topic."""
        ...

    # -- Hook runs -------------------------------------------------------------
    #
    # An accepted event is kept as one job per hook on its topic, in the same store as the hooks.
    # A webhook delivery id is claimed in the same atomic step as the jobs are written, so a
    # delivery is answered ``already_seen`` only once its runs are kept, and a failed write leaves
    # no claim behind. Every write on a claimed job is fenced on its ``claimed_by``: a writer that
    # lost the job changes nothing, logs a WARNING and gets False (or None).

    @abstractmethod
    async def _accept_runs(
        self,
        topic: str,
        jobs: list[HookRun],
        replay_claim: ReplayClaim | None,
        claimed_by: str | None,
        lease_until: float | None,
    ) -> bool:
        """Write ``jobs`` and take ``replay_claim`` on ``topic`` in one atomic step; False (nothing written) when seen.

        Each job is indexed as due on its lease when ``claimed_by`` is given, else due on its
        ``next_attempt_at``. A topic with no hooks passes no job and still takes the claim.
        """
        ...

    @abstractmethod
    async def _claim_due_runs(
        self, now: float, limit: int, claimed_by: str, lease_until: float, exclude: frozenset[str]
    ) -> list[HookRun]:
        """Claim up to ``limit`` due jobs (pending and due, or running with a lapsed lease), skipping ``exclude``.

        Each claimed job is set ``running`` under ``claimed_by`` until ``lease_until`` with one more
        attempt counted; a job that was ``running`` comes back with ``reclaimed`` set.
        """
        ...

    @abstractmethod
    async def _renew_lease(self, run_id: str, claimed_by: str, lease_until: float) -> bool:
        """Extend the lease of a job this claimer holds."""
        ...

    @abstractmethod
    async def _mark_acted(self, run_id: str, claimed_by: str, act: VisitAct, start_spec: StartSpec | None) -> bool:
        """Record a visit notice on the job.

        A ``before`` notice of a cancel or a start sets ``acted``, appends its ids to ``acted_ids``
        and writes ``start_spec`` when given; a ``before`` notice of a resume or a take writes the
        claim intent; a ``committed`` notice sets ``acted``, appends its ids and clears the intent.
        """
        ...

    @abstractmethod
    async def _clear_intent(self, run_id: str, claimed_by: str) -> bool:
        """Clear the job's claim intent, leaving ``acted`` as it is."""
        ...

    @abstractmethod
    async def _finish_run(self, run_id: str, claimed_by: str) -> bool:
        """Delete a job that succeeded (or whose condition was false)."""
        ...

    @abstractmethod
    async def _reschedule_run(
        self, run_id: str, claimed_by: str, next_attempt_at: float, error: str, kind: str
    ) -> bool:
        """Set the job pending, due at ``next_attempt_at``, with its last failure; the claim intent is kept."""
        ...

    @abstractmethod
    async def _fail_run(
        self,
        run_id: str,
        claimed_by: str,
        error: str,
        kind: str,
        failed_at: float,
        acted: HookRunActed,
        acted_ids: tuple[str, ...],
        claim_intent: ClaimIntent | None,
    ) -> HookRun | None:
        """Hold the job as failed with the attempt's own ``acted``, ``acted_ids`` and claim intent; return it."""
        ...

    @abstractmethod
    async def _release_run(self, run_id: str, claimed_by: str, now: float) -> bool:
        """Set the job pending and due at ``now``, its attempts, ``acted``, start and claim intent kept."""
        ...

    @abstractmethod
    async def list_hook_runs(
        self, status: Literal["outstanding", "failed"] | None, limit: int, cursor: str | None
    ) -> HookRunsPage:
        """One page of hook runs, ordered as :class:`HookRunsPage` states.

        ``outstanding`` lists the pending and running runs, ``failed`` the failed ones and ``None``
        every run.
        """
        ...

    @abstractmethod
    async def count_hook_runs(self) -> HookRunCounts:
        """How many hook runs are outstanding and how many are failed."""
        ...

    @abstractmethod
    async def retry_hook_run(
        self, run_id: str, now: float, *, claimed_by: str | None = None, lease_until: float | None = None
    ) -> RetryResult:
        """Re-queue a failed job as a fresh run: pending (or claimed by ``claimed_by``), ``attempts`` 0.

        The claim intent is cleared. A job whose hook started a tool keeps ``acted`` and its start
        spec; any other has ``acted`` and ``acted_ids`` reset. A job whose hook committed a resume
        or a take is ``refused`` and left as it is.
        """
        ...

    @abstractmethod
    async def discard_hook_run(self, run_id: str) -> Literal["ok", "missing", "not_failed"]:
        """Delete a failed job."""
        ...

    async def on_event(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        tool_kwargs_override: dict[str, Any] | None = None,
        dispatch_key: DispatchKey | None = None,
        replay_claim: ReplayClaim | None = None,
        monitoring_disabled: bool = False,
    ) -> AcceptResult:
        """Keep one run per hook on ``topic``, then return; the runs happen asynchronously.

        ``tool_kwargs_override`` is merged into every fired hook's tool input ABOVE the rendered
        ``expr`` input but BELOW the hook's static ``tool_kwargs``, so it supplies only the keys
        the hook's author left unpinned. ``dispatch_key`` is the trigger link's key, bound around
        every attempt. ``replay_claim`` is a webhook delivery id, claimed in the same atomic step
        as the runs are kept: a delivery already claimed keeps nothing and returns
        ``already_seen``. ``monitoring_disabled`` makes the accept and every attempt of its runs
        record nothing in monitoring.

        On the serving loop the runs are kept already claimed by this process and their first
        attempts are spawned at once. Off the serving loop, or once the shutdown stop has begun,
        they are kept unclaimed and due now, for any process's sweep.
        """
        from tai42_skeleton.hooks.runs import runtime
        from tai42_skeleton.hooks.runs.metrics import hook_run_metrics

        scope: AbstractContextManager[Any] = get_monitoring().writer.disable() if monitoring_disabled else nullcontext()
        with scope:
            writer = get_monitoring().writer
            with (
                writer.start_span(name="hook_on_event", kind=SpanKind.CHAIN),
                writer.trace_attributes(tags=[f"hook:{topic}"], metadata={"topic": topic}, name=f"event: {topic}"),
            ):
                hooks_map = await self.list_hooks_by_topic(topic)
                now = runtime.now()
                spawn = runtime.on_serving_loop() and not runtime.stopping()
                claimed_by = runtime.claimed_by() if spawn else None
                lease_until = now + self.settings.run_lease_seconds if spawn else None
                jobs = [
                    HookRun(
                        id=secrets.token_urlsafe(16),
                        topic=topic,
                        hook=name,
                        payload=payload,
                        status="running" if spawn else "pending",
                        attempts=1 if spawn else 0,
                        next_attempt_at=now,
                        created_at=now,
                        tool_kwargs_override=tool_kwargs_override,
                        dispatch_key=dispatch_key,
                        monitoring_disabled=monitoring_disabled,
                        claimed_by=claimed_by,
                        lease_until=lease_until,
                    )
                    for name in hooks_map
                ]
                if not await self._accept_runs(topic, jobs, replay_claim, claimed_by, lease_until):
                    return AcceptResult(status="already_seen", run_ids=())
                if jobs:
                    hook_run_metrics().accepted.inc(len(jobs))
                if spawn:
                    for job in jobs:
                        runtime.spawn_attempt(job)
                return AcceptResult(status="accepted", run_ids=tuple(job.id for job in jobs))


def _hook_tool_input(
    hook: HookParams, outcome: DoorContractOutcome, tool_kwargs_override: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The fired tool's input, or ``None`` when the contract starts nothing.

    ``None`` means a declared ``start_expr`` yielded null — nothing is started (the run's parked
    interactions may still be cancelled or resumed). Otherwise the fired tool's kwargs are the merge
    (strongest last) of the contract's start input (its own default empty object when the hook
    declares no ``start_expr``), the per-link override, and the hook author's static ``tool_kwargs``
    — the author's pinned keys stay unoverridable, the hook's only lock against a link minted by
    someone with no relation to the topic.
    """
    if outcome.start is None:
        return None
    # A declared ``start_expr`` yielded an object (a ``dict``) → its own kwargs; no ``start_expr``
    # (``DOOR_START_DEFAULT``) → the empty default the static ``tool_kwargs`` fill in.
    event_input = outcome.start if isinstance(outcome.start, dict) else {}
    return {**event_input, **(tool_kwargs_override or {}), **(hook.tool_kwargs or {})}


def recorded_start(
    tool: str,
    tool_input: dict[str, Any],
    run_recorded: Callable[..., Awaitable[None]],
    idempotency_key: str,
) -> Callable[[Mapping[str, Any]], Awaitable[None]]:
    """The ``visit`` start callable of a hook run: run ``tool`` recorded, keyed by the run's idempotency key.

    The ``extras`` ``visit`` hands the callable reach the started run through ``run_recorded``,
    which threads them to the target's dispatch. The idempotency key is merged last, so a door's
    ``extras_expr`` cannot overwrite it. The run is recorded with no crash-resume of its own: the
    hook run is what replays the start of a tool that declares crash-resume.
    """

    async def _start(extras: Mapping[str, Any]) -> None:
        await run_recorded(
            tool,
            tool_input,
            extras={**extras, TOOL_EXTRA_IDEMPOTENCY_KEY: idempotency_key},
            crash_resume_eligible=False,
        )

    return _start


async def _hook_state_context(hook: HookParams, payload: dict[str, Any]) -> StateContext | None:
    """The ambient ``hook``-door state context for a fire, or ``None`` when the hook declares no subject.

    The subject's ``key_expr`` runs over the event payload and MUST yield a non-empty string — an
    empty/blank/non-string key fails the fire loudly, never a silent skip — so a state write during
    the fire is keyed and attributed to the hook (actor = the hook's execution key; ``turn_id`` is
    None, a hook fire is not a conversation turn).
    """
    subject: HookSubject | None = hook.subject
    if subject is None:
        return None
    # Render the templated key_expr to its jq program IMMEDIATELY before evaluating it; a
    # by-id text whose stored resource cannot be fetched raises out of the manager and the
    # fire fails loudly like any hook error.
    key_expr = await tai42_app.storage.resource_manager.render_templated_text(subject.key_expr)
    key = await run_jq_first(key_expr, payload)
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"hook {hook.name!r} subject key_expr {key_expr!r} must yield a non-empty string, got {key!r}")
    return StateContext(
        door="hook",
        candidates=SubjectCandidates(
            target_kind=subject.target_kind, target_name=subject.target_name, by_kind={subject.kind: key}
        ),
        actor=hook.execution_key,
        turn_id=None,
    )
