"""The builtin subject-state tools: ``state_read`` / ``state_replace`` /
``state_merge`` / ``state_apply`` — LLM-facing shims over the door-agnostic
``tai42_app.states`` facet that let an agent read and write the calling subject's
document.

A *state* is a declared JSON document, one per *subject*
(``{target_kind, target_name, kind, key}``). These tools take an OPTIONAL
``subject``:

* a full ``{target_kind, target_name, kind, key}`` object addresses one subject
  outright and overrides the ambient context;
* a ``{kind, key}`` object takes its target from the door's ambient context;
* omitted, the subject is resolved entirely from the ambient context — the
  candidate the door knows for the state's ``default_subject_kind`` — so a
  conversation turn or a hook fire writes the subject it is already about without
  the agent naming it.

With no subject in scope (an explicit ``{kind, key}`` or an omitted subject and no
ambient context) the tool raises loudly rather than writing an unaddressed record.

Write provenance follows the platform's chokepoint discipline (D-6): each tool
supplies ONLY what it knows — its own name as ``consumer``, the run's session as
``run_id``, and (for ``state_apply``) the ``op_id`` — in a
:class:`~tai42_contract.states.WriteOrigin`. The ``door``, ``actor`` and
``turn_id`` are stamped by the facet from the ambient context, never here, so the
audit ledger cannot be forged by the tool.

Like every builtin the module is opt-in: a deployment names it in a manifest
``tools[].module`` entry to register the four tools.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.states import (
    StateNotFoundError,
    StateSubject,
    SubjectRefusedError,
    WriteOrigin,
)
from tai42_contract.tools import current_tool_invocation

from tai42_skeleton.tools.attribution import get_run_attribution

# The four tools return a record view / apply result as a JSON object, or ``None``
# (a ``state_read`` miss). A permissive wrap schema surfaces EVERY shape — a dict or
# a null — in ``result.data``: the server wraps it as ``{"result": <value>}`` and the
# client unwraps it back (a dict stays a dict, a null stays a null), mirroring the
# ``ask_user`` builtin's answer schema.
_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"title": "Result"}},
    "x-fastmcp-wrap-result": True,
}


def _origin(op_id: str | None = None) -> WriteOrigin:
    """The consumer-only :class:`WriteOrigin` a state tool supplies (D-6): the invoked
    tool's name as ``consumer``, the ambient run's ``session_id`` as ``run_id`` when a
    run is attributed, and the caller's ``op_id``. ``door``/``actor``/``turn_id`` are
    absent — the facet stamps them from the ambient context, never the tool."""
    invocation = current_tool_invocation()
    attribution = get_run_attribution()
    return WriteOrigin(
        consumer=invocation.tool_name if invocation is not None else None,
        meta=None,
        run_id=attribution.session_id if attribution is not None else None,
        op_id=op_id,
    )


async def _resolve_subject(state: str, subject: dict[str, Any] | None) -> StateSubject:
    """Resolve the subject a state tool addresses (D-7).

    An explicit ``{target_kind, target_name, kind, key}`` is used verbatim; an explicit
    ``{kind, key}`` takes its target from the ambient context; an omitted subject
    resolves the candidate the door knows for the state's ``default_subject_kind``.
    Every unresolvable case raises :class:`SubjectRefusedError` naming what is missing —
    never a silent unaddressed write."""
    ctx = tai42_app.states.context()
    if subject is not None:
        has_target_kind = "target_kind" in subject
        has_target_name = "target_name" in subject
        if has_target_kind != has_target_name:
            raise SubjectRefusedError(
                f"state {state!r}: an explicit subject must give both target_kind and target_name or neither"
            )
        if has_target_kind:
            return StateSubject.model_validate(subject)
        if ctx is None:
            raise SubjectRefusedError(
                f"state {state!r}: subject {subject!r} names no target and no ambient "
                "context is in scope to supply one — pass target_kind and target_name"
            )
        return StateSubject.model_validate(
            {
                "target_kind": ctx.candidates.target_kind,
                "target_name": ctx.candidates.target_name,
                **subject,
            }
        )
    if ctx is None:
        raise SubjectRefusedError(f"state {state!r}: no subject in scope: pass subject explicitly")
    declaration = await tai42_app.states.get_declaration(state)
    if declaration is None:
        raise StateNotFoundError(f"no state declared as {state!r}")
    kind = declaration.default_subject_kind
    key = ctx.candidates.by_kind.get(kind)
    if key is None:
        raise SubjectRefusedError(
            f"state {state!r}: the ambient {ctx.door!r} door resolved no subject of kind "
            f"{kind!r} (the state's default_subject_kind) — pass subject explicitly"
        )
    return StateSubject.model_validate(
        {
            "target_kind": ctx.candidates.target_kind,
            "target_name": ctx.candidates.target_name,
            "kind": kind,
            "key": key,
        }
    )


@tai42_app.tools.tool(output_schema=_RESULT_SCHEMA, tags={"states"})
async def state_read(state: str, subject: dict[str, Any] | None = None) -> Any:
    """Read the calling subject's document for a state.

    Args:
        state: The declared state's name.
        subject: The subject to address. A full
            ``{target_kind, target_name, kind, key}`` object addresses one subject
            outright; a ``{kind, key}`` object takes its target from the ambient
            context; omit it to resolve the subject entirely from the ambient context
            (the candidate the door knows for the state's ``default_subject_kind``).

    Returns:
        The record — ``{state, subject, data, seq, canonical_subject, folded_from}`` —
        or ``null`` when no record exists for the subject.

    Raises:
        SubjectRefusedError: No subject is in scope, or the subject is undeclared,
            empty-keyed, or an unknown/target-mismatched person.
        StateNotFoundError: No state is declared under ``state``.
        StatesNotConfiguredError: The states component's database is unbound.
    """
    resolved = await _resolve_subject(state, subject)
    record = await tai42_app.states.read(state, resolved)
    return record.model_dump(mode="json") if record is not None else None


@tai42_app.tools.tool(output_schema=_RESULT_SCHEMA, tags={"states"})
async def state_replace(state: str, data: dict[str, Any], subject: dict[str, Any] | None = None) -> Any:
    """Replace the calling subject's whole document for a state.

    Args:
        state: The declared state's name.
        data: The new document — a JSON object validated against the state's effective
            schema. It replaces the subject's document entirely.
        subject: The subject to address; see ``state_read``.

    Returns:
        The new record after the replace.

    Raises:
        ValueValidationError: ``data`` is not a JSON object or fails the schema.
        SubjectRefusedError: No subject is in scope, or the subject is refused.
        StateNotFoundError: No state is declared under ``state``.
        StatesNotConfiguredError: The states component's database is unbound.
    """
    resolved = await _resolve_subject(state, subject)
    record = await tai42_app.states.replace(state, resolved, data, origin=_origin())
    return record.model_dump(mode="json")


@tai42_app.tools.tool(output_schema=_RESULT_SCHEMA, tags={"states"})
async def state_merge(state: str, patch: dict[str, Any], subject: dict[str, Any] | None = None) -> Any:
    """Shallow-merge a patch into the calling subject's document for a state.

    Args:
        state: The declared state's name.
        patch: A JSON object whose top-level keys are set on the subject's document
            (one ``set`` op per key); existing keys not named are left untouched.
        subject: The subject to address; see ``state_read``.

    Returns:
        The new record after the merge.

    Raises:
        ValueValidationError: ``patch`` is not a JSON object or the result fails the
            schema.
        SubjectRefusedError: No subject is in scope, or the subject is refused.
        StateNotFoundError: No state is declared under ``state``.
        StatesNotConfiguredError: The states component's database is unbound.
    """
    resolved = await _resolve_subject(state, subject)
    record = await tai42_app.states.merge(state, resolved, patch, origin=_origin())
    return record.model_dump(mode="json")


@tai42_app.tools.tool(output_schema=_RESULT_SCHEMA, tags={"states"})
async def state_apply(
    state: str,
    ops: list[dict[str, Any]],
    subject: dict[str, Any] | None = None,
    op_id: str | None = None,
) -> Any:
    """Apply a batch of path operations to the calling subject's document for a state.

    Args:
        state: The declared state's name.
        ops: The ordered operations, each ``{"op": ..., "path": [...], ...}`` (e.g.
            ``set``/``remove`` and the keyed collection ops). A whole-path write over a
            ``composing`` path is refused before anything is committed.
        subject: The subject to address; see ``state_read``.
        op_id: An optional idempotency key — a replayed ``op_id`` returns
            ``applied=false`` without re-writing.

    Returns:
        The apply result — ``{applied, data, seq, skipped}`` — with the resulting
        document when it applied and the guard-skipped ops otherwise.

    Raises:
        InvalidPathError: ``ops`` is not a list or an op path is malformed.
        RegimeViolationError: An op's shape violates a mounted path's regime.
        ValueValidationError: The document fails the schema after the ops.
        SubjectRefusedError: No subject is in scope, or the subject is refused.
        StateNotFoundError: No state is declared under ``state``.
        StatesNotConfiguredError: The states component's database is unbound.
    """
    resolved = await _resolve_subject(state, subject)
    result = await tai42_app.states.apply(state, resolved, ops, op_id=op_id, origin=_origin(op_id))
    return result.model_dump(mode="json")
