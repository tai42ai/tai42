"""Resolving a state's ``template_jq`` programs, reading what they run over, and routing what they give.

This module resolves, reads and routes; what a program gives is the kit's
(:mod:`tai42_kit.states.template_jq`). An ``input`` program's value is returned; an ``update``
program's op batch, rebased under the attachment path, is applied through the same ``apply``
chokepoint as any delta.
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Literal

from psycopg import AsyncConnection
from tai42_contract.states import TemplateJqReference
from tai42_contract.states.errors import StateNotFoundError, ValueValidationError
from tai42_contract.states.models import (
    CompletedOrigin,
    StateSubject,
    TemplateJqApplyResult,
    TemplateJqResult,
    WriteOrigin,
)
from tai42_kit.states.template_jq import (
    BoundProgram,
    derive_update_ops,
    evaluate_input_program,
    select_template_jq,
)

from tai42_skeleton.states.outbox.drain import drained
from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.unit import current_state_unit
from tai42_skeleton.states.templates import StateTemplate

# ``(template, template_version, path, parameters, declarations, program_name)`` of a resolved program.
ResolvedProgram = tuple[StateTemplate, int, list[str], dict[str, Any], dict[str, Any], str]


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

        :meth:`resolve_template_jq_batch` for the one reference, raising its refusal.
        """
        ref = TemplateJqReference(state=state, name=name, purpose=purpose, declared=list(declared))
        (result,) = await self.resolve_template_jq_batch([ref])
        if isinstance(result, (StateNotFoundError, ValueValidationError)):
            raise result
        return result

    async def resolve_template_jq_batch(
        self, refs: Sequence[TemplateJqReference]
    ) -> list[ResolvedProgram | StateNotFoundError | ValueValidationError]:
        """Resolve every reference in ``refs``, each at its state's current declaration version.

        One statement reads every named state's version; each reference then resolves by
        :meth:`_resolve_template_jq`'s rules on that version's catalog snapshot. Returns one item per
        reference, in order: the resolved program, or the :class:`StateNotFoundError` /
        :class:`ValueValidationError` refusing it (an undeclared state is a :class:`StateNotFoundError`).
        """
        self._ensure_available()
        if not refs:
            return []
        versions = await self._store.declaration_versions(sorted({ref.state for ref in refs}))
        results: list[ResolvedProgram | StateNotFoundError | ValueValidationError] = []
        for ref in refs:
            version = versions.get(ref.state)
            if version is None:
                results.append(StateNotFoundError(f"no state declared as {ref.state!r}"))
                continue
            try:
                results.append(
                    await self._resolve_template_jq(
                        ref.state, version, ref.name, purpose=ref.purpose, declared=ref.declared
                    )
                )
            except (StateNotFoundError, ValueValidationError) as exc:
                results.append(exc)
        return results

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

    async def _bound_program(self, resolved: ResolvedProgram) -> BoundProgram:
        """The resolved program rendered from the rendered-template cache and bound to its attachment.

        A body that cannot be fetched or rendered raises loudly.
        """
        template, version, path, parameters, declarations, program_name = resolved
        rendered = await self._rendered(template, version)
        return BoundProgram(
            template=template.name,
            name=program_name,
            params=list(template.template_jq[program_name].params),
            body=rendered.bodies[program_name],
            prelude=rendered.sibling_prelude,
            path=path,
            parameters=parameters,
            declarations=declarations,
        )

    async def _record_data(
        self, state: str, subject: StateSubject, conn: AsyncConnection[Any] | None
    ) -> Mapping[str, Any] | None:
        """``subject``'s record document — a bound unit's projection when one is bound — or ``None`` with no record."""
        record = await self._projected_record_view(state, subject, conn=conn)
        return record["data"] if record is not None else None

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
        program = await self._bound_program(await self._resolve_template_jq(state, version, name, purpose="input"))
        value = await evaluate_input_program(program, args, lambda: self._record_data(state, subject, conn))
        return TemplateJqResult(name=name, value=value)

    async def _resolve_template_jq_ops(
        self,
        state: str,
        subject: StateSubject,
        name: str,
        input_: Any,
        *,
        conn: AsyncConnection[Any] | None = None,
        admit_erasing: bool = False,
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
        :class:`ValueValidationError`. ``admit_erasing`` is the outbox replay's: a person under its
        erase mark is admitted.
        """
        version, _kinds, _default = await self._admit_subject(state, subject, admit_erasing=admit_erasing)
        program = await self._bound_program(await self._resolve_template_jq(state, version, name, purpose="update"))
        return await derive_update_ops(program, input_, lambda: self._record_data(state, subject, conn))

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
        completed = self._complete_origin(origin)
        if conn is not None:
            return await self._apply_template_jq_completed(
                state, subject, name, input_, op_id=op_id, origin=completed, conn=conn
            )
        # A pending save met by the write lands first, and the program runs again over it.
        return await drained(
            self,
            lambda: self._apply_template_jq_completed(state, subject, name, input_, op_id=op_id, origin=completed),
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
        admit_erasing: bool = False,
    ) -> TemplateJqApplyResult:
        """The program's ops applied under an already-completed origin.

        ``validate=False`` leaves the whole-document check to the caller; ``admit_erasing`` (the outbox
        replay's) admits a person under its erase mark at both of the write's admissions.
        """
        ops = await self._resolve_template_jq_ops(state, subject, name, input_, conn=conn, admit_erasing=admit_erasing)
        applied = await self._apply_completed(
            state, subject, ops, op_id=op_id, origin=origin, conn=conn, validate=validate, admit_erasing=admit_erasing
        )
        return TemplateJqApplyResult(
            name=name, applied=applied.applied, data=applied.data, seq=applied.seq, skipped=applied.skipped
        )
