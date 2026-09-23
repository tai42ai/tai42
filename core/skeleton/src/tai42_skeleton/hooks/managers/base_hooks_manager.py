"""The abstract hooks-manager base: hook registration, event fan-out, and replay defense."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tai42_kit.interactions import DoorContractOutcome

from tai42_contract.app import tai42_app
from tai42_contract.hooks.models import HookParams, HookSubject
from tai42_contract.interactions.door_contract import PARKED_VARIABLE
from tai42_contract.monitoring import MonitoringLevel, SpanKind
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_contract.template import TemplatedText
from tai42_kit.interactions import evaluate_door_contract, parked_entries_for_jq
from tai42_kit.utils.data import run_jq_first
from tai42_kit.utils.data.jq_util import compile_check, get_compiled_jq

from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.monitoring import get_monitoring
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.states.context import state_context

logger = logging.getLogger(__name__)


class BaseHooksManager(ABC):
    """Base for hook managers: registration, per-topic verifier bindings, and event fan-out.

    Backends supply the storage of hooks, verifier bindings, and the replay seen-set; this base
    supplies the shared firing, condition checks, and the manager-wide concurrency bound.
    """

    def __init__(self, settings: HooksSettings):
        """Store ``settings`` and size the manager-wide in-flight execution semaphore."""
        self.settings = settings
        # One semaphore for the manager's lifetime, bounding TOTAL in-flight hook
        # executions across ALL events at ``settings.max_workers`` — concurrent
        # events share the same bound rather than each fanning out its own.
        self._run_semaphore = asyncio.Semaphore(settings.max_workers)

    @staticmethod
    def validate_jq_fields(params: HookParams) -> None:
        """Reject inline jq that does not compile, at registration time.

        A broken condition or door-contract expression would otherwise surface only as a hook
        that never fires (indistinguishable from a false condition). A templated text naming a
        stored resource renders per event and cannot be compiled here — its failures surface
        loudly at fire time instead. The four door-contract expressions read the run's parked
        interactions as ``$parked``, so they are compiled with that variable declared.
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
    async def _run_hook(hook: HookParams, payload: dict[str, Any], tool_kwargs_override: dict[str, Any] | None = None):
        """Fire one hook's tool AS the hook's bound execution key.

        The bind must stay HERE, inside the per-hook coroutine: a contextvar set inside a
        task is invisible to its siblings, which is what gives each fanned-out hook its
        own key rather than a sibling's or the server's unbounded authority.
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
                    start = _hook_start(hook, outcome, tool_kwargs_override, run_recorded)
                    # ``receives_outcome=False``: a hook is a receiver-less door — a started target
                    # that async-parks is subject-tracked, and a ``resume_expr`` resume of a
                    # ``to="caller"`` entry fires the run's own delivery rather than returning inline.
                    # ``visit`` deposits ``state_binding`` around ``start`` alone (never around a
                    # resume's continuation), so the hook keeps no ambient tool-invocation deposit.
                    await tai42_app.interactions.visit(
                        target_name=hook.tool,
                        cancel=outcome.cancel,
                        resume=outcome.resume,
                        start=start,
                        extras=outcome.extras,
                        state_binding=hook.state_binding,
                        receives_outcome=False,
                    )

    async def _run_hook_with_limit(
        self, hook: HookParams, payload: dict[str, Any], tool_kwargs_override: dict[str, Any] | None = None
    ):
        async with self._run_semaphore:
            await self._run_hook(hook, payload, tool_kwargs_override)

    @abstractmethod
    async def register(self, params: HookParams) -> bool:
        """Register the hook described by ``params``; return whether it was newly added."""
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
    # never a secret value. Both manager backends store the same shape behind these
    # four methods.

    @abstractmethod
    async def set_topic_verifier(self, topic: str, binding: dict[str, Any]) -> None:
        """Store the webhook-verifier ``binding`` for ``topic``."""
        ...

    @abstractmethod
    async def get_topic_verifier(self, topic: str) -> dict[str, Any] | None:
        """Return the webhook-verifier binding for ``topic``, or ``None`` when none is set."""
        ...

    @abstractmethod
    async def delete_topic_verifier(self, topic: str) -> bool:
        """Remove ``topic``'s webhook-verifier binding; return whether one was removed."""
        ...

    @abstractmethod
    async def all_topic_verifiers(self) -> dict[str, dict[str, Any]]:
        """Return every topic's webhook-verifier binding, keyed by topic."""
        ...

    # -- Webhook replay defense (seen-set) -----------------------------------
    #
    # The public ingress fan-out re-fires every hook bound to a topic on each
    # delivery, so a captured validly-signed delivery would re-fire them all on
    # replay. A verifier that yields a per-delivery ``SeenSetClaim`` is deduped
    # here: the FIRST delivery of an id is claimed and passes, a replay within the
    # window is refused. The claim and its TTL are ONE atomic op — never a claim
    # without a TTL, which would leak a permanent key.

    @abstractmethod
    async def claim_webhook_delivery(self, topic: str, replay_key: str, ttl_seconds: int) -> bool:
        """Atomically claim a delivery id for ``topic``.

        Returns ``True`` on the FIRST claim (a legitimate first delivery — proceed), ``False`` when
        the id was already claimed within ``ttl_seconds`` (a replay — the caller returns the
        idempotent already-seen response and dispatches nothing). Raises on a non-positive
        ``ttl_seconds`` — a claim is never taken without a bounded TTL.
        """
        ...

    async def on_event(
        self, topic: str, payload: dict[str, Any], *, tool_kwargs_override: dict[str, Any] | None = None
    ):
        """Fan the event out to every hook on ``topic``.

        ``tool_kwargs_override`` (kw-only) is merged into every fired hook's tool input
        ABOVE the rendered ``expr`` input but BELOW the hook's static ``tool_kwargs``, so
        it supplies only the keys the hook's author left unpinned.

        Each hook runs in its own task and binds its own execution identity there, so a
        hook is never fired under a sibling's key; a denied fire is that hook's error
        outcome and leaves the rest of the fan-out untouched.
        """
        writer = get_monitoring().writer
        with (
            writer.start_span(name="hook_on_event", kind=SpanKind.CHAIN),
            writer.trace_attributes(tags=[f"hook:{topic}"], metadata={"topic": topic}, name=f"event: {topic}"),
        ):
            hooks_map = await self.list_hooks_by_topic(topic)
            if not hooks_map:
                return

            valid_hooks = [hook for hook in hooks_map.values() if await self._check_condition(hook, payload)]
            if not valid_hooks:
                return

            # Every execution goes through the manager-wide semaphore, so this
            # event's fan-out shares the global in-flight bound with every
            # concurrently firing event.
            tasks = [self._run_hook_with_limit(h, payload, tool_kwargs_override) for h in valid_hooks]

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for hook, result in zip(valid_hooks, results, strict=True):
                if isinstance(result, BaseException):
                    logger.error(
                        "hook %r on topic %r failed: %s",
                        hook.name,
                        topic,
                        result,
                        exc_info=result,
                    )


def _hook_start(
    hook: HookParams,
    outcome: DoorContractOutcome,
    tool_kwargs_override: dict[str, Any] | None,
    run_recorded: Callable[..., Awaitable[None]],
) -> Callable[[Mapping[str, Any]], Awaitable[None]] | None:
    """The ``visit`` start callable for a hook fire, or ``None`` when the contract starts nothing.

    ``None`` means a declared ``start_expr`` yielded null — nothing is started (the run's parked
    interactions may still be cancelled or resumed). Otherwise the fired tool's kwargs are the merge
    (strongest last) of the contract's start input (its own default empty object when the hook
    declares no ``start_expr``), the per-link override, and the hook author's static ``tool_kwargs``
    — the author's pinned keys stay unoverridable, the hook's only lock against a link minted by
    someone with no relation to the topic. The ``extras`` ``visit`` hands the callable reach the
    started run through ``run_recorded``, which threads them to the target's dispatch.
    """
    if outcome.start is None:
        return None
    # A declared ``start_expr`` yielded an object (a ``dict``) → its own kwargs; no ``start_expr``
    # (``DOOR_START_DEFAULT``) → the empty default the static ``tool_kwargs`` fill in.
    event_input = outcome.start if isinstance(outcome.start, dict) else {}
    tool_input = {**event_input, **(tool_kwargs_override or {}), **(hook.tool_kwargs or {})}

    async def _start(extras: Mapping[str, Any]) -> None:
        await run_recorded(hook.tool, tool_input, extras=extras)

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
