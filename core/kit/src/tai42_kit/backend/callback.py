"""Callback glue — chain a follow-up tool after a backend task runs.

:class:`CallbackSchema` completes the contract field shape with render methods
that reach the host's resource manager. ``callback_execution`` gates a task
result on the rendered condition, transforms it with the rendered expression,
and optionally runs a follow-up tool. ``prepare_backend_kwargs`` strips the
FastMCP context and injects the tool name before a backend dispatch.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_contract.access_control.context import caller_may_read_secrets
from tai42_contract.app import tai42_app
from tai42_contract.backend import CallbackSchema as CallbackFields
from tai42_contract.states import StateSubject

from tai42_kit.backend.schedule_fire import visit_return
from tai42_kit.utils.data import run_jq_first
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run
from tai42_kit.utils.lc.signature_util import exclude_fastmcp_ctx_from_kwargs
from tai42_kit.utils.render import render_templated_text
from tai42_kit.utils.schedule_subject import (
    SCHEDULE_EXECUTION_FINGERPRINT_ARG,
    SCHEDULE_EXECUTION_KEY_ARG,
    SCHEDULE_STAMPED_DOOR_OPTS,
    SCHEDULE_STATE_BINDING_ARG,
    SCHEDULE_SUBJECT_ARG,
    assert_no_reserved_schedule_keys,
    assert_schedule_create_fire,
    pop_schedule_execution_identity,
    pop_schedule_subject,
    schedule_subject_context,
)
from tai42_kit.utils.worker_secret_capability import (
    WORKER_SECRET_CAPABILITY_ARG,
    access_control_gate_state,
    bind_worker_secret_capability,
)


class CallbackSchema(CallbackFields):
    """The contract callback field shape plus render methods that reach the live resource manager."""

    async def rendered_condition(self) -> str:
        """The condition template rendered against live resources; ``""`` when none is set."""
        # No condition is an empty condition: the execution gate treats "" as "run".
        return await render_templated_text(self.condition) if self.condition is not None else ""

    async def rendered_expr(self) -> str:
        """The expression template rendered against live resources; ``""`` when none is set."""
        # No expression is an empty expression: the execution path yields {} for it.
        return await render_templated_text(self.expr) if self.expr is not None else ""


async def prepare_backend_kwargs(
    func: Callable[..., Any], tool_name_arg: str, tool_name: str, kwargs: dict[str, Any], *, scheduled: bool = False
) -> dict[str, Any]:
    """Prepare a backend dispatch's kwargs: strip the context, inject the tool name, stamp the capability.

    Stamps the submitting caller's secret-read capability so the worker binds it for
    the job. Runs in the submitter's request context, so :func:`caller_may_read_secrets` reads the
    submitter's own admin verdict; stamped AFTER the caller's arguments are stripped, so a
    caller can never forge a higher capability.

    With ``scheduled=True`` the create door has already stamped the job's subject under
    :data:`SCHEDULE_SUBJECT_ARG` (one of :data:`SCHEDULE_STAMPED_DOOR_OPTS`, reaching this branch
    through the widened signature) so the worker fire can re-establish a ``schedule`` state context
    the anonymous/system fire otherwise loses; ``kwargs["subject"]``, if present, is the tool's own
    argument and is forwarded untouched. A reserved top-level ``state_binding`` argument (the door
    binding the create door injected) is re-stamped under :data:`SCHEDULE_STATE_BINDING_ARG` and the
    raw key is POPPED — the binding must never reach the base tool, so the worker fire is its only
    reader (tools stay pure).

    With ``scheduled=False`` (a background task tool) any caller-supplied reserved schedule-door key is
    REFUSED first (:func:`assert_no_reserved_schedule_keys`) — a task tool's caller never supplies one,
    and the platform stamps the ambient fire only after this check, so a forged key can never ride the
    job to the worker's ``backend_fire`` pop. The ambient door subject and firing identity, when one is
    bound, are then forwarded onto the job (:func:`_forward_ambient_fire`) so the deferred fire
    re-establishes them; a submit with no ambient door context stamps nothing and runs plainly.

    The ``scheduled=True`` path instead PROVES it was entered through the create door
    (:func:`assert_schedule_create_fire`): that door refuses caller-supplied reserved keys and stamps
    the validated door signals this path carries, then dispatches the branch inside its create fire, so
    a second key-refusal here would reject the platform's own stamps. A caller who names the
    ``<tool>_schedule_task`` branch directly at the run-tool/MCP edge reaches this path with no create
    fire on the stack and is refused loudly, so a forged reserved key can never ride the job to the fire.
    """
    kwargs = exclude_fastmcp_ctx_from_kwargs(func, kwargs)
    kwargs[tool_name_arg] = tool_name
    kwargs[WORKER_SECRET_CAPABILITY_ARG] = caller_may_read_secrets()
    if scheduled:
        assert_schedule_create_fire()
        state_binding = kwargs.pop("state_binding", None)
        if state_binding is not None:
            kwargs[SCHEDULE_STATE_BINDING_ARG] = state_binding
        # The branch signature declares the create-door-stamped reserved keys so the dispatch
        # validates; makefun materialises any the create door did NOT stamp as ``None``. Drop those
        # absent Nones so a plain schedule's stored job carries no reserved door signal — only a real
        # subject / firing identity / door contract rides to the worker's ``backend_fire`` pop.
        for key in SCHEDULE_STAMPED_DOOR_OPTS:
            if kwargs.get(key) is None:
                kwargs.pop(key, None)
    else:
        assert_no_reserved_schedule_keys(kwargs)
        _forward_ambient_fire(kwargs)
    return kwargs


def _forward_ambient_fire(kwargs: dict[str, Any]) -> None:
    """Stamp the ambient door subject + firing identity onto ``kwargs`` so a deferred fire re-establishes them.

    A background task tool dispatched from within a door fire (a schedule/hook run with a bound
    subject and identity) forwards that pair onto its worker job so the deferred fire keys its state
    writes and rebinds its parks under the SAME subject and authority — the ``backend_fire`` seam
    re-establishes them at the fire. Only a single-key ambient subject with a bound identity forwards;
    with none, nothing is stamped and the job runs as a plain background task.
    """
    from tai42_kit.utils.state_context import current_state_context

    context = current_state_context()
    subject = _candidates_subject(context)
    if subject is None:
        return
    identity = tai42_app.interactions.current_fire_identity()
    if identity is None:
        return
    kwargs[SCHEDULE_SUBJECT_ARG] = subject.model_dump()
    kwargs[SCHEDULE_EXECUTION_KEY_ARG], kwargs[SCHEDULE_EXECUTION_FINGERPRINT_ARG] = identity


def _candidates_subject(context: Any) -> StateSubject | None:
    """The ambient state context's single-key subject as a :class:`StateSubject`, or ``None``.

    A door fire keys on one subject, so an ambient context with exactly one ``by_kind`` entry yields a
    forwardable subject; a context-free run, or an ambiguous multi-key one, yields ``None`` (nothing
    is forwarded rather than guessing which key the deferred fire should re-key on).
    """
    if context is None:
        return None
    by_kind = context.candidates.by_kind
    if len(by_kind) != 1:
        return None
    ((kind, key),) = by_kind.items()
    return StateSubject(
        target_kind=context.candidates.target_kind,
        target_name=context.candidates.target_name,
        kind=kind,
        key=key,
    )


def carry_forwarded_fire(callback: CallbackSchema | dict[str, Any], kwargs: dict[str, Any]) -> None:
    """Carry the callback job's secret-read capability and the task's forwarded door pair onto its spec.

    The callback runs as a SEPARATE job that receives only the predecessor's result and this spec, so
    what it needs rides on the spec's ``carried_kwargs``. It ALWAYS carries the secret-read capability
    under :data:`WORKER_SECRET_CAPABILITY_ARG`: the access-control gate state decided here, in the
    submitting process (gate ON -> ``False``, gate OFF -> ``True``) — a follow-up no caller
    re-authorizes never clears the secret fence off the submitter's own verdict. A background task
    dispatched from within a door fire also forwards its subject/identity pair onto its worker job
    (:func:`_forward_ambient_fire`); that pair is carried too, or the follow-up loses the door context
    (:func:`_run_callback_tool` reads it back). Each enqueue path calls this once with its popped
    ``callback_kwargs`` option — a :class:`CallbackSchema` or the raw mapping the spec is before it
    crosses the queue as JSON.
    """
    carried: dict[str, Any] = {
        key: kwargs[key]
        for key in (SCHEDULE_SUBJECT_ARG, SCHEDULE_EXECUTION_KEY_ARG, SCHEDULE_EXECUTION_FINGERPRINT_ARG)
        if key in kwargs
    }
    carried[WORKER_SECRET_CAPABILITY_ARG] = not access_control_gate_state()
    if isinstance(callback, CallbackSchema):
        callback.carried_kwargs = carried
    else:
        callback["carried_kwargs"] = carried


async def callback_execution(result: Any, callback: CallbackSchema) -> Any:
    """Run ``callback`` over ``result``: gate on the condition, transform, then run the follow-up tool."""
    cond = await callback.rendered_condition()
    if cond:
        # An empty pipeline is falsy → skip, matching the ``if not cond_output`` gate.
        cond_output = await run_jq_first(cond, result, default=None)
        if not cond_output:
            return None

    expr = await callback.rendered_expr()
    # Empty expr is not an error: ``get_compiled_jq("")`` raises, so it yields {}.
    # An empty PIPELINE from a non-empty expr also yields {} (default). Evaluated
    # through ``run_jq_first`` so the JQ_TIMEOUT_SECONDS budget holds.
    expr_output = (await run_jq_first(expr, result, default={})) if expr else {}

    if callback.tool:
        # A worker executes a dequeued callback with no live caller holding a
        # connection, so the turn budget does not apply, and no HTTP request bound
        # the secret-read capability — the worker binds the one the callback carried.
        capability = _carried_capability(callback)
        detached_token = mark_detached_run()
        try:
            with bind_worker_secret_capability(capability):
                return await _run_callback_tool(callback, expr_output)
        finally:
            reset_detached_run(detached_token)
    return expr_output


def _carried_capability(callback: CallbackSchema) -> bool:
    """Read the secret-read capability the enqueue path carried; a spec without one raises ``KeyError``.

    The spec is left as it is: a backend that retries a callback re-runs the same spec object, and the
    retry must find the capability again.
    """
    try:
        return callback.carried_kwargs[WORKER_SECRET_CAPABILITY_ARG]
    except KeyError:
        raise KeyError(f"callback spec carries no {WORKER_SECRET_CAPABILITY_ARG!r}") from None


async def _run_callback_tool(callback: CallbackSchema, expr_output: Any) -> Any:
    """Run ``callback.tool`` with ``expr_output``, re-establishing a forwarded door context when carried.

    With no forwarded pair in :attr:`CallbackSchema.carried_kwargs` the tool runs as a plain follow-up,
    with no door context. With a forwarded subject/identity the callback re-binds the followed run's
    firing identity and deposits its ``schedule`` subject context, then drives the tool as a plain
    start through the shared ``visit`` (receiver-less) — so a follow-up that async-asks is
    subject-tracked; a follow-up that asks with NO forwarded identity fails closed loudly at its own
    park (no identity to rebind the continuation as), never a silent unresumable park.
    """
    carried = dict(callback.carried_kwargs)
    subject = pop_schedule_subject(carried)
    identity = pop_schedule_execution_identity(carried)

    if subject is None and identity is None:
        return await tai42_app.tools.run_tool(callback.tool, expr_output, offload_sync=True)

    async def _drive() -> Any:
        with schedule_subject_context(subject):
            outcome = await tai42_app.interactions.visit(
                target_name=callback.tool,
                cancel=[],
                resume=[],
                start=lambda extras: tai42_app.tools.run_tool(
                    callback.tool, expr_output, offload_sync=True, extras=extras
                ),
                extras={},
                state_binding=None,
                receives_outcome=False,
            )
        return visit_return(outcome)

    if identity is not None:
        user_id, fingerprint = identity
        async with tai42_app.interactions.bound_execution_identity_for_fire(user_id, fingerprint):
            return await _drive()
    return await _drive()
