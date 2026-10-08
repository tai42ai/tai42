"""The door-layer state-binding runtime: merge, apply-before/apply-after, and validate-and-attach at save.

ONE binding shape rides every door (:class:`~tai42_contract.states.StateBinding`). It reaches
the shared dispatch chokepoint through the ambient :class:`~tai42_contract.tools.ToolInvocation`
(a door deposits its own binding; the chokepoint carries it forward and merges it with the
dispatched preset's own binding), and this module is the ONE place the merged binding is
applied — injections into the run input BEFORE the dispatch, updates through the store AFTER
it. Building it here, at the single seam every door flows through, keeps per-door copies from
drifting. Tools never see the binding.

Errors are loud: an unknown state/template, an injection whose jq fails, an update whose apply
fails, a subject that cannot be resolved — each raises and the run's outcome carries it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from tai42_contract.states import (
    AttachBody,
    StateAttach,
    StateBatchWrite,
    StateBinding,
    StateSubject,
    WriteOrigin,
)
from tai42_contract.states.errors import (
    AttachConflictError,
    InvalidPathError,
    RegimeViolationError,
    SchemaValidationError,
    StateNotFoundError,
    StatesError,
    SubjectRefusedError,
    TemplateValidationError,
    ValueValidationError,
)
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data import run_jq_first
from tai42_kit.utils.data.jq_util import compile_check

from tai42_skeleton.states.service.unit import current_state_unit
from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError, TemplateNotFoundError

if TYPE_CHECKING:
    from tai42_skeleton.app.server import TaiMCP


# The store errors that refuse a binding's own content. Matched by exact type: a subclass or any other
# StatesError (a store fault, an unbound store) is not a refusal and propagates.
BINDING_REFUSALS: Final[frozenset[type[StatesError]]] = frozenset(
    {
        SubjectRefusedError,
        SchemaValidationError,
        InvalidPathError,
        ValueValidationError,
        RegimeViolationError,
        TemplateValidationError,
        StateNotFoundError,
        AttachConflictError,
    }
)


def is_binding_refusal(exc: BaseException) -> bool:
    """Whether ``exc`` refuses the binding's content (exact type), as opposed to a store or transport failure."""
    return type(exc) in BINDING_REFUSALS


@dataclass(frozen=True)
class DeferredBinding:
    """The merged binding, the run input it saw, and the door id a parking run carries to its terminal.

    Deposited around the OUTERMOST dispatch (after the binding's injections ran) so a run that
    PARKS deep inside can capture the exact binding, input and door the live apply would have used
    — then apply the UPDATES once at its real terminal instead of dropping them on the pause.
    """

    binding: StateBinding
    run_input: dict[str, Any]
    door_id: str


_deferred_binding: ContextVar[DeferredBinding | None] = ContextVar("tai42_deferred_binding", default=None)


def current_deferred_binding() -> DeferredBinding | None:
    """The merged binding, run input and door id the outermost dispatch deposited, or ``None`` under no binding."""
    return _deferred_binding.get()


@contextmanager
def deferred_binding_scope(binding: StateBinding, run_input: dict[str, Any], door_id: str) -> Iterator[None]:
    """Deposit the merged binding (+ its run input + door id) for the span of the outermost dispatch.

    A nested park captures it from :func:`current_deferred_binding`; the token is reset on exit so it
    never leaks past the dispatch.
    """
    token = _deferred_binding.set(DeferredBinding(binding, run_input, door_id))
    try:
        yield
    finally:
        _deferred_binding.reset(token)


async def _render_slot(app: TaiMCP, slot: str, text: TemplatedText) -> str:
    """Render one binding jq slot's templated text to its jq program before it is compiled or evaluated.

    The render happens HERE at the door, never inside a contract model validator.
    A slot given by ``id`` is fetched and rendered; a stored id that cannot be fetched is a
    LOUD refusal naming the slot and the id, never a silent pass or a deferred surprise.
    """
    try:
        return await app.storage.resource_manager.render_templated_text(text)
    except (TemplateNotFoundError, TemplateLocaleNotFoundError) as exc:
        raise ValueValidationError(
            f"state binding {slot} references stored id {text.id!r}, which could not be fetched: {exc}"
        ) from exc


def merge_bindings(door: StateBinding | None, preset: StateBinding | None) -> StateBinding | None:
    """Merge the door's binding with the dispatched preset's own into ONE binding applied once around the run.

    ``None`` on either side yields the other unchanged.

    DOOR-FIRST: door :class:`StateAttach` entries come first, then the preset's. For a state
    named by BOTH, ``subject_expr`` is always the DOOR's; ``scope_expr`` is the door's when
    present, else the preset's; ``templates`` are UNIONED (door order first, deduplicated);
    and ``input_injections``/``updates`` are CONCATENATED door-first then preset. No cross-check
    between the two definitions — each validated on its own save.
    """
    if door is None:
        return preset
    if preset is None:
        return door
    order: list[str] = []
    merged: dict[str, StateAttach] = {}
    for attach in (*door.states, *preset.states):
        existing = merged.get(attach.state)
        if existing is None:
            merged[attach.state] = attach
            order.append(attach.state)
            continue
        templates = list(dict.fromkeys([*existing.templates, *attach.templates]))
        merged[attach.state] = existing.model_copy(
            update={
                "templates": templates,
                # ``subject_expr`` is required, so the DOOR's (``existing``) always wins;
                # ``scope_expr`` is optional, so the door's wins WHEN PRESENT, else the preset's.
                "scope_expr": existing.scope_expr if existing.scope_expr is not None else attach.scope_expr,
                "input_injections": [*existing.input_injections, *attach.input_injections],
                "updates": [*existing.updates, *attach.updates],
            }
        )
    return StateBinding(states=[merged[name] for name in order])


async def _scope_engaged(app: TaiMCP, attach: StateAttach, run_input: dict[str, Any]) -> bool:
    """Return whether ``attach`` engages for this run, from its optional ``scope_expr`` boolean predicate.

    ``scope_expr`` renders to a boolean over the run input: absent engages, ``true`` engages,
    ``false`` SKIPS the state for this run (no injections/updates), and any non-boolean result
    is a loud refusal (never a silent skip).
    """
    if attach.scope_expr is None:
        return True
    expr = await _render_slot(app, f"scope_expr for state {attach.state!r}", attach.scope_expr)
    verdict = await run_jq_first(expr, run_input)
    if not isinstance(verdict, bool):
        raise ValueValidationError(
            f"state binding scope_expr for state {attach.state!r} must yield a boolean, got {verdict!r}"
        )
    return verdict


async def _resolve_subject(app: TaiMCP, attach: StateAttach, run_input: dict[str, Any]) -> StateSubject:
    """Resolve the record subject for ``attach`` from its ``subject_expr`` over the run input.

    ``subject_expr`` must yield something: a jq ``null`` is a loud refusal here. Any other value
    is a subject reference the platform's :meth:`app.states.resolve_subject` resolves — a full
    subject object, a ``{kind, key}`` object or a bare key string under the ambient
    :class:`~tai42_contract.states.StateContext` — refusing an unresolvable one with
    :class:`~tai42_contract.states.SubjectRefusedError`.
    """
    expr = await _render_slot(app, f"subject_expr for state {attach.state!r}", attach.subject_expr)
    resolved = await run_jq_first(expr, run_input)
    if resolved is None:
        raise ValueValidationError(
            f"state binding subject_expr for state {attach.state!r} must yield a non-empty key string or a full "
            f"subject object, got {resolved!r}"
        )
    return await app.states.resolve_subject(attach.state, resolved)


async def apply_binding_injections(app: TaiMCP, binding: StateBinding, arguments: dict[str, Any]) -> None:
    """Inject each engaged attach's ``input_injections`` into ``arguments`` in place BEFORE the dispatch.

    A named ``template_jq`` (input purpose) is evaluated over the subject's record with NO
    params (a named injection carries no param values — a params-declaring input jq is a loud
    refusal); a custom ``jq`` runs over the record (its ``.``) with the run input bound as
    ``$input``. The value lands at ``into``. An attach whose ``scope_expr`` predicate is
    ``false`` is skipped.
    """
    for attach in binding.states:
        if not await _scope_engaged(app, attach, arguments):
            continue
        subject = await _resolve_subject(app, attach, arguments)
        for injection in attach.input_injections:
            if injection.template_jq is not None:
                result = await app.states.eval_template_jq(attach.state, subject, injection.template_jq, {})
                value = result.value
            else:
                if injection.jq is None:
                    raise AssertionError
                record = await app.states.read(attach.state, subject)
                data = record.data if record is not None else {}
                jq = await _render_slot(app, f"injection jq for state {attach.state!r}", injection.jq)
                value = await run_jq_first(jq, data, variables={"input": arguments})
            arguments[injection.into] = value


async def _resolve_op_id(
    app: TaiMCP, state: str, op_id_expr: TemplatedText | None, run_input: dict[str, Any], output: Any
) -> str | None:
    """Resolve an update's optional ``op_id`` idempotency key from its rendered expression over the tool output.

    ``.`` is the tool output; the run input is bound as ``$input``. ``null`` means no key. A
    non-string, non-null result is a loud refusal.
    """
    if op_id_expr is None:
        return None
    expr = await _render_slot(app, f"op_id expression for state {state!r}", op_id_expr)
    value = await run_jq_first(expr, output, variables={"input": run_input})
    if value is None or isinstance(value, str):
        return value
    raise ValueValidationError(f"state binding op_id expression must yield a string or null, got {value!r}")


async def _attach_update_writes(
    app: TaiMCP,
    attach: StateAttach,
    subject: StateSubject,
    arguments: dict[str, Any],
    output: Any,
    door_id: str,
    *,
    idempotency_scope: str | None,
    base_index: int,
) -> list[StateBatchWrite]:
    """Build one engaged attach's ``updates`` into its write set over the tool ``output``.

    The node-entry record is read ONCE and every custom update authors ``$record`` against that one
    snapshot. A deferred apply (``idempotency_scope`` set) keys each author-op-id-less write on
    ``<scope>:<index>`` — its position in the whole binding's write set — so a redelivery replays the
    same ops idempotently.
    """
    writes: list[StateBatchWrite] = []
    record_data: dict[str, Any] | None = None
    for update in attach.updates:
        op_id = await _resolve_op_id(app, attach.state, update.op_id, arguments, output)
        if op_id is None and idempotency_scope is not None:
            op_id = f"tai42:park-binding:{idempotency_scope}:{base_index + len(writes)}"
        if update.template_jq is not None:
            if update.adapter is not None:
                adapter = await _render_slot(app, f"update adapter for state {attach.state!r}", update.adapter)
                adapted = await run_jq_first(adapter, output, variables={"input": arguments})
            else:
                adapted = arguments
            writes.append(
                StateBatchWrite(
                    state=attach.state,
                    subject=subject,
                    template_jq=update.template_jq,
                    input=adapted,
                    op_id=op_id,
                    origin=WriteOrigin(consumer="template_jq", meta={"template_jq": update.template_jq}),
                )
            )
            continue
        if update.jq is None:
            raise AssertionError
        if record_data is None:
            record = await app.states.read(attach.state, subject)
            record_data = record.data if record is not None else {}
        jq = await _render_slot(app, f"update jq for state {attach.state!r}", update.jq)
        ops = await run_jq_first(jq, output, variables={"input": arguments, "record": record_data})
        if not isinstance(ops, list):
            raise ValueValidationError(
                f"state binding custom update jq for state {attach.state!r} must return an op batch "
                f"(a list), got {type(ops).__name__}"
            )
        writes.append(
            StateBatchWrite(
                state=attach.state,
                subject=subject,
                ops=ops,
                op_id=op_id,
                origin=WriteOrigin(consumer=f"door:{door_id}"),
            )
        )
    return writes


async def apply_binding_updates(
    app: TaiMCP,
    binding: StateBinding,
    arguments: dict[str, Any],
    output: Any,
    door_id: str,
    *,
    idempotency_scope: str | None = None,
) -> None:
    """Build every engaged attach's ``updates`` into ONE write set and apply it as ONE transaction after the dispatch.

    A named ``template_jq`` (update purpose) shapes its ``.input`` from the ``adapter`` over the
    tool output (its ``.``) with the run input bound as ``$input`` (or the run input directly
    when it declares no adapter) and is queued as a ``template_jq`` write; a custom ``jq`` authors
    the whole op batch over the tool output (its ``.``) with the run input bound as ``$input`` and
    the record as ``$record``, queued as an ``ops`` write. The node-entry record is read ONCE per
    engaged attach, and every custom update of that attach authors ``$record`` against that one
    snapshot; the ops then land sequentially and atomically inside the single transaction the
    batch applies. Every queued item across every engaged attach is applied through
    :meth:`app.states.apply_batch` ONCE, so a node's whole update set commits or rolls back
    together — a failed update rolls the node's writes back.

    Single-writer identity: a TEMPLATE update writes as one door-independent writer keyed on
    its resolved program name (the SAME update from any door on one state is one writer); a
    CUSTOM update writes as the door's own writer (``door_id`` = the dispatched definition).
    An attach whose ``scope_expr`` predicate is ``false`` is skipped.

    While a states unit of work is bound to the caller's scope the write set STAGES into it rather
    than landing in the store — so these binding writes read back within the scope and roll back
    with a discard, exactly as the facet's own read seam serves a bound unit; with no unit open the
    set applies directly, as one transaction, through :meth:`app.states.apply_batch`.

    ``idempotency_scope`` makes a DEFERRED apply (a parked run's updates applied at its real
    terminal) land exactly once under at-least-once delivery: every item that carries no author
    ``op_id`` is keyed on ``<scope>:<index>`` so a redelivery re-driving the same terminal replays
    the same ops the ledger already recorded (``ON CONFLICT DO NOTHING``). The live apply passes
    ``None`` (one dispatch, one apply) and is unchanged.
    """
    items: list[StateBatchWrite] = []
    for attach in binding.states:
        if not await _scope_engaged(app, attach, arguments):
            continue
        subject = await _resolve_subject(app, attach, arguments)
        items.extend(
            await _attach_update_writes(
                app,
                attach,
                subject,
                arguments,
                output,
                door_id,
                idempotency_scope=idempotency_scope,
                base_index=len(items),
            )
        )
    unit = current_state_unit()
    if unit is not None:
        await unit.stage(items)
    else:
        await app.states.apply_batch(items)


async def validate_and_attach_binding(app: TaiMCP, binding: StateBinding) -> None:
    """Validate a binding and ATTACH its named templates at SAVE (the write of the runnable definition carrying it).

    Attach-on-use: each named template is attached at its own path ``[<template>]`` — shared by
    every door/node that binds the state — IDEMPOTENTLY (an already-attached template is left
    as is; a second template at an occupied path is refused by ``attach``). An attach failure
    raises and the save fails. Then each authored slot (subject/scope exprs, custom
    injection/update jqs, op-id exprs, adapters) is RENDERED to its jq program — a by-id slot
    whose resource cannot be fetched fails the save loudly, naming the slot and the id — and
    the rendered program is compiled; every named ``template_jq`` referenced by an
    injection/update is resolved against the state's attachments with the right purpose — an
    unknown/ambiguous name, or a named update that names params but carries no adapter to fill
    them, is a loud refusal at save.
    """
    await _validate_binding(app, binding, do_attach=True)


async def validate_binding(app: TaiMCP, binding: StateBinding) -> None:
    """Run the SAME validation as :func:`validate_and_attach_binding`, but WITHOUT attaching.

    The dry-run (preset validate) door performs NO attach. Each declared template is verified
    to EXIST (proving it could be attached) rather than actually being attached, and named
    ``template_jq`` references resolve against the state's current attachments UNION the
    binding's own declared templates. A bad shape/state/template/jq/adapter is the same loud
    refusal, so the validate verdict matches what a create/save would accept.
    """
    await _validate_binding(app, binding, do_attach=False)


async def _validate_templates(app: TaiMCP, attach: StateAttach, *, do_attach: bool) -> None:
    """Per declared template of ``attach``: attach it idempotently (SAVE) or assert it exists (dry-run).

    Attach-on-use, idempotent: skip an already-attached template (``attach`` would 409).
    A dry run performs no attach — it only asserts the template exists (is attachable).
    """
    for template in attach.templates:
        already = await app.states.list_attachments(attach.state, template=template)
        if already:
            continue
        if do_attach:
            await app.states.attach(attach.state, template, AttachBody(path=[template]))
        elif await app.states.get_template(template) is None:
            raise StateNotFoundError(f"template {template!r} to attach on state {attach.state!r} does not exist")


async def _validate_injections(app: TaiMCP, attach: StateAttach) -> None:
    """Compile each injection's custom jq, or resolve each named ``template_jq`` to an ``"input"`` program."""
    for injection in attach.input_injections:
        if injection.jq is not None:
            compile_check(
                await _render_slot(app, f"injection jq for state {attach.state!r}", injection.jq),
                variables=["input"],
            )
        else:
            if injection.template_jq is None:
                raise AssertionError
            await app.states.resolve_template_jq(
                attach.state, injection.template_jq, purpose="input", declared=attach.templates
            )


async def _validate_updates(app: TaiMCP, attach: StateAttach) -> None:
    """Compile op_id/custom-jq, resolve each named update ``template_jq``, and enforce the adapter-params rule."""
    for update in attach.updates:
        if update.op_id is not None:
            compile_check(
                await _render_slot(app, f"op_id expression for state {attach.state!r}", update.op_id),
                variables=["input"],
            )
        if update.jq is not None:
            compile_check(
                await _render_slot(app, f"update jq for state {attach.state!r}", update.jq),
                variables=["input", "record"],
            )
        else:
            if update.template_jq is None:
                raise AssertionError
            program = await app.states.resolve_template_jq(
                attach.state, update.template_jq, purpose="update", declared=attach.templates
            )
            if update.adapter is not None:
                compile_check(
                    await _render_slot(app, f"update adapter for state {attach.state!r}", update.adapter),
                    variables=["input"],
                )
            elif program.params:
                raise ValueValidationError(
                    f"state binding update {update.template_jq!r} on state {attach.state!r} declares params "
                    f"{program.params} but the binding carries no adapter to fill its input"
                )


async def _validate_binding(app: TaiMCP, binding: StateBinding, *, do_attach: bool) -> None:
    """Run the shared binding validation for both the save and dry-run seams.

    With ``do_attach`` the named templates are attached idempotently (the SAVE seam); without
    it they are only verified to exist (the dry-run seam) — the one difference between the two
    doors, so neither drifts from the other's verdict.
    """
    for attach in binding.states:
        await _validate_templates(app, attach, do_attach=do_attach)
        compile_check(await _render_slot(app, f"subject_expr for state {attach.state!r}", attach.subject_expr))
        if attach.scope_expr is not None:
            compile_check(await _render_slot(app, f"scope_expr for state {attach.state!r}", attach.scope_expr))
        await _validate_injections(app, attach)
        await _validate_updates(app, attach)
