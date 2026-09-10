"""The door-layer state-binding runtime: merge, apply-before / apply-after, and the
save-time validate-and-mount seam.

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

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from tai42_contract.states import (
    AttachBody,
    StateAttach,
    StateBinding,
    StateSubject,
    WriteOrigin,
)
from tai42_contract.states.errors import StateNotFoundError, ValueValidationError
from tai42_kit.utils.data import run_jq_first

if TYPE_CHECKING:
    from tai42_skeleton.app.server import TaiMCP


def merge_bindings(door: StateBinding | None, preset: StateBinding | None) -> StateBinding | None:
    """Merge the door's binding with the dispatched preset's own into ONE binding, applied
    once around the run. ``None`` on either side yields the other unchanged.

    DOOR-FIRST: door :class:`StateAttach` entries come first, then the preset's. For a state
    named by BOTH, ``subject_expr`` is always the DOOR's; ``scope_expr`` is the door's when
    present, else the preset's; ``templates`` are UNIONED (door order first, deduplicated);
    and ``input_injections``/``updates`` are CONCATENATED door-first then preset. No cross-check
    between the two definitions — each validated on its own save."""
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


async def _scope_engaged(attach: StateAttach, run_input: dict[str, Any]) -> bool:
    """Whether ``attach`` engages for this run — its ``scope_expr`` is an optional BOOLEAN
    predicate over the run input: absent engages, ``true`` engages, ``false`` SKIPS the state
    for this run (no injections/updates), and any non-boolean result is a loud refusal (never
    a silent skip)."""
    if attach.scope_expr is None:
        return True
    verdict = await run_jq_first(attach.scope_expr, run_input)
    if not isinstance(verdict, bool):
        raise ValueValidationError(
            f"state binding scope_expr for state {attach.state!r} must yield a boolean, got {verdict!r}"
        )
    return verdict


async def _resolve_subject(app: TaiMCP, attach: StateAttach, run_input: dict[str, Any]) -> StateSubject:
    """Resolve the record subject for ``attach`` from its ``subject_expr`` over the run input.

    ``subject_expr`` yields either a FULL subject object (``{target_kind, target_name, kind,
    key}``) or a bare KEY string. A key string takes its scope ``(target_kind, target_name)``
    from the ambient :class:`~tai42_contract.states.StateContext` the door deposited and its
    ``kind`` from the state's ``default_subject_kind``. Any other shape, or a key with no
    ambient scope, is a loud refusal — never a silent skip."""
    resolved = await run_jq_first(attach.subject_expr, run_input)
    if isinstance(resolved, dict):
        return StateSubject.model_validate(resolved)
    if not isinstance(resolved, str) or not resolved.strip():
        raise ValueValidationError(
            f"state binding subject_expr for state {attach.state!r} must yield a non-empty key string or a full "
            f"subject object, got {resolved!r}"
        )
    ctx = app.states.context()
    if ctx is None:
        raise ValueValidationError(
            f"state binding subject_expr for state {attach.state!r} yielded key {resolved!r} but no ambient "
            f"subject scope is set — yield a full subject object instead"
        )
    decl = await app.states.get_declaration(attach.state)
    if decl is None:
        raise StateNotFoundError(f"no state declared as {attach.state!r}")
    return StateSubject(
        target_kind=ctx.candidates.target_kind,
        target_name=ctx.candidates.target_name,
        kind=decl.default_subject_kind,
        key=resolved,
    )


async def apply_binding_injections(app: TaiMCP, binding: StateBinding, arguments: dict[str, Any]) -> None:
    """Inject each engaged attach's ``input_injections`` into ``arguments`` (in place) BEFORE
    the dispatch. A named ``template_jq`` (input purpose) is evaluated over the subject's
    record with NO params (a named injection carries no param values — a params-declaring
    input jq is a loud refusal); a custom ``jq`` runs over ``{record, input}``. The value
    lands at ``into``. An attach whose ``scope_expr`` predicate is ``false`` is skipped."""
    for attach in binding.states:
        if not await _scope_engaged(attach, arguments):
            continue
        subject = await _resolve_subject(app, attach, arguments)
        for injection in attach.input_injections:
            if injection.template_jq is not None:
                result = await app.states.eval_template_jq(attach.state, subject, injection.template_jq, {})
                value = result.value
            else:
                assert injection.jq is not None  # the model sets exactly one of template_jq/jq
                record = await app.states.read(attach.state, subject)
                data = record.data if record is not None else {}
                value = await run_jq_first(injection.jq, {"record": data, "input": arguments})
            arguments[injection.into] = value


async def _resolve_op_id(op_id_expr: str | None, run_input: dict[str, Any], output: Any) -> str | None:
    """An update's optional ``op_id`` idempotency key from its expression over
    ``{output, input}``; ``null`` means no key. A non-string, non-null result is a loud
    refusal."""
    if op_id_expr is None:
        return None
    value = await run_jq_first(op_id_expr, {"output": output, "input": run_input})
    if value is None or isinstance(value, str):
        return value
    raise ValueValidationError(f"state binding op_id expression must yield a string or null, got {value!r}")


async def apply_binding_updates(
    app: TaiMCP, binding: StateBinding, arguments: dict[str, Any], output: Any, door_id: str
) -> None:
    """Apply each attach's ``updates`` through the store AFTER the dispatch. A named
    ``template_jq`` (update purpose) shapes its ``.input`` from the ``adapter`` over
    ``{output, input}`` (or the run input directly when it declares none) and is applied via
    :meth:`app.states.apply_template_jq`; a custom ``jq`` authors the whole op batch over
    ``{record, output, input}`` and is applied via :meth:`app.states.apply`.

    Single-writer identity: a TEMPLATE update writes as one door-independent writer keyed on
    its resolved program name (the SAME update from any door on one state is one writer); a
    CUSTOM update writes as the door's own writer (``door_id`` = the dispatched definition).
    An attach whose ``scope_expr`` predicate is ``false`` is skipped."""
    for attach in binding.states:
        if not await _scope_engaged(attach, arguments):
            continue
        subject = await _resolve_subject(app, attach, arguments)
        for update in attach.updates:
            op_id = await _resolve_op_id(update.op_id, arguments, output)
            if update.template_jq is not None:
                if update.adapter is not None:
                    adapted = await run_jq_first(update.adapter, {"output": output, "input": arguments})
                else:
                    adapted = arguments
                await app.states.apply_template_jq(
                    attach.state,
                    subject,
                    update.template_jq,
                    adapted,
                    op_id=op_id,
                    origin=WriteOrigin(consumer="template_jq", meta={"template_jq": update.template_jq}),
                )
            else:
                assert update.jq is not None  # the model sets exactly one of template_jq/jq
                record = await app.states.read(attach.state, subject)
                data = record.data if record is not None else {}
                ops = await run_jq_first(update.jq, {"record": data, "output": output, "input": arguments})
                if not isinstance(ops, list):
                    raise ValueValidationError(
                        f"state binding custom update jq for state {attach.state!r} must return an op batch "
                        f"(a list), got {type(ops).__name__}"
                    )
                await app.states.apply(
                    attach.state, subject, ops, op_id=op_id, origin=WriteOrigin(consumer=f"door:{door_id}")
                )


async def validate_and_mount_binding(app: TaiMCP, binding: StateBinding) -> None:
    """Validate a binding and MOUNT its named templates at SAVE (the write of the runnable
    definition carrying it).

    Mount-on-use: each named template is attached at its own path ``[<template>]`` — shared by
    every door/node that binds the state — IDEMPOTENTLY (an already-attached template is left
    as is; a second template at an occupied path is refused by ``attach``). A mount failure
    raises and the save fails. Then each expression is compiled (subject/scope exprs, custom
    injection/update jqs, op-id exprs, adapters), and every named ``template_jq`` referenced by
    an injection/update is resolved against the state's attachments with the right purpose — an
    unknown/ambiguous name, or a named update that names params but carries no adapter to fill
    them, is a loud refusal at save."""
    await _validate_binding(app, binding, mount=True)


async def validate_binding(app: TaiMCP, binding: StateBinding) -> None:
    """The SAME validation :func:`validate_and_mount_binding` runs, but WITHOUT mounting —
    the dry-run (preset validate) door performs NO attach. Each declared template is verified
    to EXIST (so it could be mounted) instead of being attached, and named ``template_jq``
    references resolve against the state's current attachments UNION the binding's own declared
    templates. A bad shape/state/template/jq/adapter is the same loud refusal, so the validate
    verdict matches what a create/save would accept."""
    await _validate_binding(app, binding, mount=False)


async def _validate_binding(app: TaiMCP, binding: StateBinding, *, mount: bool) -> None:
    """Shared binding validation. With ``mount`` the named templates are attached
    idempotently (the SAVE seam); without it they are only verified to exist (the dry-run
    seam) — the one difference between the two doors, so neither drifts from the other's
    verdict."""
    from tai42_kit.utils.data.jq_util import compile_check

    for attach in binding.states:
        # Mount-on-use, idempotent: skip an already-attached template (``attach`` would 409).
        # A dry run performs no attach — it only asserts the template exists (is mountable).
        for template in attach.templates:
            already = await app.states.list_attachments(attach.state, template=template)
            if already:
                continue
            if mount:
                await app.states.attach(attach.state, template, AttachBody(path=[template]))
            elif await app.states.get_template(template) is None:
                raise StateNotFoundError(f"template {template!r} to mount on state {attach.state!r} does not exist")
        compile_check(attach.subject_expr)
        if attach.scope_expr is not None:
            compile_check(attach.scope_expr)
        for injection in attach.input_injections:
            if injection.jq is not None:
                compile_check(injection.jq)
            else:
                assert injection.template_jq is not None  # the model sets exactly one source
                await _require_program(app, attach.state, injection.template_jq, "input", declared=attach.templates)
        for update in attach.updates:
            if update.op_id is not None:
                compile_check(update.op_id)
            if update.jq is not None:
                compile_check(update.jq)
            else:
                assert update.template_jq is not None  # the model sets exactly one source
                program = await _require_program(
                    app, attach.state, update.template_jq, "update", declared=attach.templates
                )
                if update.adapter is not None:
                    compile_check(update.adapter)
                elif program.get("params"):
                    raise ValueValidationError(
                        f"state binding update {update.template_jq!r} on state {attach.state!r} declares params "
                        f"{program['params']} but the binding carries no adapter to fill its input"
                    )


async def _require_program(
    app: TaiMCP, state: str, name: str, purpose: str, *, declared: Iterable[str] = ()
) -> dict[str, Any]:
    """Resolve a named ``template_jq`` across ``state``'s attachments and assert its purpose;
    return its document entry (so the caller can read declared ``params``). An unknown or
    ambiguous name, or a wrong purpose, is a loud refusal. ``declared`` names the binding's own
    would-be-mounted templates: on the SAVE seam they are already attached and appear here
    anyway, so this only matters to the dry-run seam, where a self-mounted template resolves
    without being attached."""
    attachments = await app.states.list_attachments(state)
    templates = {row["template"] for row in attachments} | set(declared)
    target, program_name = name.split(".", 1) if "." in name else (None, name)
    matches: list[dict[str, Any]] = []
    for template_name in templates:
        if target is not None and template_name != target:
            continue
        doc = await app.states.get_template(template_name)
        entry = (doc.template_jq or {}).get(program_name) if doc is not None else None
        if entry is not None:
            matches.append(entry)
    if target is not None and target not in templates:
        raise StateNotFoundError(f"template {target!r} is not attached on state {state!r}")
    if not matches:
        raise StateNotFoundError(f"no template_jq {name!r} on any template attached on state {state!r}")
    if len(matches) > 1:
        raise ValueValidationError(f"template_jq {name!r} is declared by more than one template on state {state!r}")
    entry = matches[0]
    if entry.get("purpose") != purpose:
        raise ValueValidationError(
            f"template_jq {name!r} on state {state!r} has purpose {entry.get('purpose')!r}, needs {purpose!r}"
        )
    return entry
