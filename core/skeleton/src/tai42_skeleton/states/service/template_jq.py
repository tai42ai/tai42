"""Resolving, rendering and running a state's ``template_jq`` programs.

An ``input`` program evaluates over a subject's attached subtree and returns a value; an
``update`` program returns a template-relative op batch that is rebased under the attachment
path and applied through the same ``apply`` chokepoint as any delta.
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Literal

from psycopg import AsyncConnection
from tai42_contract.states.errors import StateNotFoundError, ValueValidationError
from tai42_contract.states.models import (
    CompletedOrigin,
    StateSubject,
    TemplateJqApplyResult,
    TemplateJqResult,
    WriteOrigin,
)
from tai42_kit.utils.data.jq_util import run_jq_first

from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.reconcile_support import _rebase_op, _record_subtree
from tai42_skeleton.states.service.unit import current_state_unit
from tai42_skeleton.states.templates import StateTemplate

# ``(template, template_version, path, parameters, declarations, program_name)`` of a resolved program.
ResolvedProgram = tuple[StateTemplate, int, list[str], dict[str, Any], dict[str, Any], str]


def select_template_jq(
    state: str, name: str, purpose: str, candidates: Sequence[tuple[str, Mapping[str, Any]]]
) -> tuple[int, str]:
    """Pick the program a ``template_jq`` reference ``name`` names among ``candidates``.

    ``candidates`` are ``(template name, its programs by name)`` pairs, each program carrying a
    ``purpose``; returns ``(index of the declaring candidate, program name)``. A QUALIFIED
    ``<template>.<name>`` picks that template's program — a template not among the candidates, or
    one that declares no such program, is a :class:`StateNotFoundError`. An UNQUALIFIED name picks
    the one candidate that declares it — none is a :class:`StateNotFoundError`, two or more a
    :class:`ValueValidationError` (the caller qualifies it). A program whose purpose is not
    ``purpose`` is a :class:`ValueValidationError`.
    """
    if "." in name:
        template_name, program_name = name.split(".", 1)
        matches = [i for i, (candidate, _programs) in enumerate(candidates) if candidate == template_name]
        if not matches:
            raise StateNotFoundError(f"template {template_name!r} is not attached on state {state!r}")
        if program_name not in candidates[matches[0]][1]:
            raise StateNotFoundError(
                f"template {template_name!r} attached on state {state!r} declares no template_jq {program_name!r}"
            )
    else:
        program_name = name
        matches = [i for i, (_candidate, programs) in enumerate(candidates) if name in programs]
        if not matches:
            raise StateNotFoundError(f"no template_jq {name!r} on any template attached on state {state!r}")
        if len(matches) > 1:
            templates = ", ".join(sorted(candidates[i][0] for i in matches))
            raise ValueValidationError(
                f"template_jq {name!r} is declared by more than one template attached on state {state!r} "
                f"({templates}); qualify it as <template>.{name}"
            )
    index = matches[0]
    actual = candidates[index][1][program_name].purpose
    if actual != purpose:
        raise ValueValidationError(
            f"template_jq {program_name!r} on template {candidates[index][0]!r} has purpose {actual!r}, "
            f"needs {purpose!r}"
        )
    return index, program_name


class _TemplateJqMixin(_StatesServiceBase):
    async def resolve_template_jq(
        self,
        state: str,
        name: str,
        *,
        purpose: Literal["input", "update"],
        declared: Collection[str] = (),
    ) -> ResolvedProgram:
        """Resolve the ``template_jq`` reference ``name`` on ``state`` at its current declaration version.

        The rules are :meth:`_resolve_template_jq`'s; an undeclared state is a :class:`StateNotFoundError`.
        """
        self._ensure_available()
        version = await self._store.declaration_version(state)
        if version is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        return await self._resolve_template_jq(state, version, name, purpose=purpose, declared=declared)

    async def _resolve_template_jq(
        self,
        state: str,
        version: int,
        name: str,
        *,
        purpose: Literal["input", "update"],
        declared: Collection[str] = (),
    ) -> ResolvedProgram:
        """Resolve a ``template_jq`` program ``name`` across ``state``'s attachments at declaration ``version``.

        The candidates are the state's attached templates plus each ``declared`` template not attached
        on the state, taking part as if attached at ``[<template>]`` with empty parameters and
        declarations (a declared template that does not exist is a :class:`StateNotFoundError`);
        :func:`select_template_jq` picks the program by its rules.
        """
        entry = await self._catalog.catalog_entry_at(state, version)
        if entry is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        candidates: list[tuple[StateTemplate, int, list[str], dict[str, Any], dict[str, Any]]] = [
            (
                await self._template_at(a.template, a.template_version, a.body),
                a.template_version,
                list(a.path),
                copy.deepcopy(a.parameters),
                copy.deepcopy(a.declarations),
            )
            for a in entry.attachments
        ]
        attached = {a.template for a in entry.attachments}
        for template_name in dict.fromkeys(declared):
            if template_name in attached:
                continue
            template_entry = await self._template_entry(template_name)
            if template_entry is None:
                raise StateNotFoundError(f"no template {template_name!r}")
            template = await self._template_at(template_name, template_entry.version, template_entry.body)
            candidates.append((template, template_entry.version, [template_name], {}, {}))
        index, program_name = select_template_jq(
            state, name, purpose, [(c[0].name, c[0].template_jq) for c in candidates]
        )
        template, template_version, path, parameters, declarations = candidates[index]
        return template, template_version, path, parameters, declarations, program_name

    async def _render_template_jq(self, template: StateTemplate, version: int, program_name: str) -> tuple[str, str]:
        """``program_name``'s rendered body and the sibling input-program prelude, from the rendered-template cache.

        A by-id body that cannot be fetched raises loudly.
        """
        rendered = await self._rendered(template, version)
        return rendered.bodies[program_name], rendered.sibling_prelude

    async def eval_template_jq(
        self,
        state: str,
        subject: StateSubject,
        name: str,
        args: dict[str, Any],
        *,
        conn: AsyncConnection[Any] | None = None,
    ) -> TemplateJqResult:
        """Evaluate an ``input``-purpose ``template_jq`` program ``name`` over ``subject``'s record.

        Returns its value. ``args`` supplies the program's declared ``params`` as the single
        ``$params`` object (every declared key must be present — value may be null — and an
        undeclared key is a loud refusal); the jq runs over the record's attached subtree with
        the attachment's ``$parameters``/``$declarations`` bound and the sibling ``tjq_<name>``
        input-program prelude. Read-only. An ``update``-purpose name is a
        :class:`ValueValidationError`.
        """
        self._ensure_available()
        version, _kinds, _default = await self._admit_subject(state, subject)
        template, template_version, path, parameters, declarations, program_name = await self._resolve_template_jq(
            state, version, name, purpose="input"
        )
        program = template.template_jq[program_name]
        missing = sorted(set(program.params) - set(args))
        unknown = sorted(set(args) - set(program.params))
        if missing or unknown:
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} takes params {program.params}"
                + (f", missing {missing}" if missing else "")
                + (f", got unknown {unknown}" if unknown else "")
            )
        record = await self._projected_record_view(state, subject, conn=conn)
        subtree = _record_subtree(record["data"], path) if record is not None else {}
        variables: dict[str, Any] = {"parameters": parameters, "declarations": declarations, "params": dict(args)}
        body, prelude = await self._render_template_jq(template, template_version, program_name)
        try:
            value = await run_jq_first(body, subtree, prelude=prelude, variables=variables)
        except Exception as exc:
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} failed to evaluate: {exc}"
            ) from exc
        return TemplateJqResult(name=name, value=value)

    async def _resolve_template_jq_ops(
        self,
        state: str,
        subject: StateSubject,
        name: str,
        input_: Any,
        *,
        conn: AsyncConnection[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve an ``update``-purpose ``template_jq`` program ``name`` to the op batch it authors for ``subject``.

        Runs the program's jq over the record's attached subtree (its ``.``) — served from a bound
        unit's projection when one is bound, else the committed store — with ``$input`` and the
        attachment's ``$parameters``/``$declarations`` bound and the sibling ``tjq_<name>``
        input-program prelude, and returns the template-relative op batch rebased under the
        attachment path. The apply-vs-stage caller applies or projects those ops. When the program
        DECLARES ``params`` they are the contract for its ``.input`` object: ``input`` must be an
        object carrying exactly those keys (a value may be null) — a missing or undeclared key is a
        loud :class:`ValueValidationError`; a program that declares none accepts any ``input``. An
        ``input``-purpose name, or a jq that does not return an op batch, is a
        :class:`ValueValidationError`.
        """
        version, _kinds, _default = await self._admit_subject(state, subject)
        template, template_version, path, parameters, declarations, program_name = await self._resolve_template_jq(
            state, version, name, purpose="update"
        )
        program = template.template_jq[program_name]
        if program.params:
            if not isinstance(input_, dict):
                raise ValueValidationError(
                    f"template_jq {program_name!r} on template {template.name!r} declares params "
                    f"{program.params}, so its input must be an object, got {type(input_).__name__}"
                )
            missing = sorted(set(program.params) - set(input_))
            unknown = sorted(set(input_) - set(program.params))
            if missing or unknown:
                raise ValueValidationError(
                    f"template_jq {program_name!r} on template {template.name!r} declares params {program.params}"
                    + (f", input missing {missing}" if missing else "")
                    + (f", input has undeclared {unknown}" if unknown else "")
                )
        record = await self._projected_record_view(state, subject, conn=conn)
        subtree = _record_subtree(record["data"], path) if record is not None else {}
        variables: dict[str, Any] = {"parameters": parameters, "declarations": declarations, "input": input_}
        body, prelude = await self._render_template_jq(template, template_version, program_name)
        try:
            result = await run_jq_first(
                body,
                subtree,
                prelude=prelude,
                variables=variables,
            )
        except Exception as exc:
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} failed to evaluate: {exc}"
            ) from exc
        if not isinstance(result, list):
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} is an update program, so its jq must "
                f"return an op batch (a list of ops), got {type(result).__name__}"
            )
        return [_rebase_op(op, path) for op in result]

    async def apply_template_jq(
        self,
        state: str,
        subject: StateSubject,
        name: str,
        input_: Any,
        *,
        op_id: str | None,
        origin: WriteOrigin,
        conn: AsyncConnection[Any] | None = None,
    ) -> TemplateJqApplyResult:
        """Apply an ``update``-purpose ``template_jq`` program ``name`` to ``subject``.

        Its jq runs over the record's attached subtree (its ``.``) with the adapter's ``input``
        bound as ``$input`` and the attachment's ``$parameters``/``$declarations`` bound and the
        sibling ``tjq_<name>`` input-program prelude, returning a template-relative op batch
        rebased under the attachment path and applied through the SAME ``apply`` chokepoint as
        a delta — so regimes, the composing-shape guard, retention, trace stamping and
        ``op_id`` idempotency all hold identically. When the program DECLARES ``params`` they
        are the contract for its ``.input`` object: ``input`` must be an object carrying
        exactly those keys (a value may be null) — a missing or undeclared key is a loud
        :class:`ValueValidationError`; a program that declares none accepts any ``input``. An
        ``input``-purpose name, or a jq that does not return an op batch, is a
        :class:`ValueValidationError`.
        """
        self._ensure_available()
        if conn is None and current_state_unit() is not None:
            # A bound unit stages the program's ops through the ``apply`` door.
            ops = await self._resolve_template_jq_ops(state, subject, name, input_)
            applied = await self.apply(state, subject, ops, op_id=op_id, origin=origin)
            return TemplateJqApplyResult(
                name=name, applied=applied.applied, data=applied.data, seq=applied.seq, skipped=applied.skipped
            )
        return await self._apply_template_jq_completed(
            state, subject, name, input_, op_id=op_id, origin=self._complete_origin(origin), conn=conn
        )

    async def _apply_template_jq_completed(
        self,
        state: str,
        subject: StateSubject,
        name: str,
        input_: Any,
        *,
        op_id: str | None,
        origin: CompletedOrigin,
        conn: AsyncConnection[Any] | None = None,
        validate: bool = True,
    ) -> TemplateJqApplyResult:
        """The program's ops applied under an already-completed origin.

        ``validate=False`` leaves the whole-document check to the caller.
        """
        ops = await self._resolve_template_jq_ops(state, subject, name, input_, conn=conn)
        applied = await self._apply_completed(
            state, subject, ops, op_id=op_id, origin=origin, conn=conn, validate=validate
        )
        return TemplateJqApplyResult(
            name=name, applied=applied.applied, data=applied.data, seq=applied.seq, skipped=applied.skipped
        )
