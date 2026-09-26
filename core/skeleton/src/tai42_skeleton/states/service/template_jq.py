"""Resolving, rendering and running a state's ``template_jq`` programs.

An ``input`` program evaluates over a subject's attached subtree and returns a value; an
``update`` program returns a template-relative op batch that is rebased under the attachment
path and applied through the same ``apply`` chokepoint as any delta.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import StateNotFoundError, ValueValidationError
from tai42_contract.states.models import StateSubject, TemplateJqApplyResult, TemplateJqResult, WriteOrigin
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.jq_util import run_jq_first

from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.reconcile_support import _rebase_op, _record_subtree
from tai42_skeleton.states.templates import StateTemplate, template_jq_prelude
from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError, TemplateNotFoundError


class _TemplateJqMixin(_StatesServiceBase):
    async def _resolve_template_jq(
        self, state: str, name: str
    ) -> tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any], str]:
        """Resolve a ``template_jq`` program ``name`` to its attachment tuple across ``state``'s attachments.

        Returns ``(template, path, parameters, declarations, program_name)``. An UNQUALIFIED name
        resolves to the one attachment whose template declares it — a name two attached templates
        both declare is a loud :class:`ValueValidationError` (the caller qualifies it). A
        QUALIFIED ``<template>.<name>`` resolves to that attachment's template — a template not
        attached on the state is a loud :class:`StateNotFoundError`. An unknown program is a
        :class:`StateNotFoundError`.
        """
        attachments = await self._load_state_attachments(state)
        if "." in name:
            template_name, program_name = name.split(".", 1)
            for template, path, parameters, declarations in attachments:
                if template.name == template_name:
                    if program_name not in template.template_jq:
                        raise StateNotFoundError(
                            f"template {template_name!r} attached on state {state!r} declares no "
                            f"template_jq {program_name!r}"
                        )
                    return template, path, parameters, declarations, program_name
            raise StateNotFoundError(f"template {template_name!r} is not attached on state {state!r}")
        matches = [
            (template, path, parameters, declarations)
            for template, path, parameters, declarations in attachments
            if name in template.template_jq
        ]
        if not matches:
            raise StateNotFoundError(f"no template_jq {name!r} on any template attached on state {state!r}")
        if len(matches) > 1:
            templates = ", ".join(sorted(t.name for t, _p, _pa, _d in matches))
            raise ValueValidationError(
                f"template_jq {name!r} is declared by more than one template attached on state {state!r} "
                f"({templates}); qualify it as <template>.{name}"
            )
        template, path, parameters, declarations = matches[0]
        return template, path, parameters, declarations, name

    async def _render_program_body(self, template: StateTemplate, program_name: str, text: TemplatedText) -> str:
        """Render one ``template_jq`` program body to its jq text just before it is compiled or evaluated.

        The render happens HERE, never in a validator. A by-id body whose stored resource cannot
        be fetched is a LOUD refusal naming the program and the id.
        """
        try:
            return await tai42_app.storage.resource_manager.render_templated_text(text)
        except (TemplateNotFoundError, TemplateLocaleNotFoundError) as exc:
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} references stored id {text.id!r}, "
                f"which could not be fetched: {exc}"
            ) from exc

    async def _render_template_jq(self, template: StateTemplate, program_name: str) -> tuple[str, str]:
        """Render ``program_name``'s body and the sibling input-program prelude to jq text before it runs.

        Every INPUT-purpose sibling is rendered (the prelude needs each body); the target
        program's own body is rendered too (reusing the input render when it is itself an input
        program). A by-id body that cannot be fetched raises loudly here.
        """
        rendered_inputs: dict[str, str] = {}
        for other_name, other in template.template_jq.items():
            if other.purpose == "input":
                rendered_inputs[other_name] = await self._render_program_body(template, other_name, other.jq)
        prelude = template_jq_prelude(rendered_inputs)
        body = rendered_inputs.get(program_name)
        if body is None:
            body = await self._render_program_body(template, program_name, template.template_jq[program_name].jq)
        return body, prelude

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
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        template, path, parameters, declarations, program_name = await self._resolve_template_jq(state, name)
        program = template.template_jq[program_name]
        if program.purpose != "input":
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} has purpose {program.purpose!r}; "
                f"eval needs an 'input'-purpose program (apply an 'update' one instead)"
            )
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
        body, prelude = await self._render_template_jq(template, program_name)
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
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        template, path, parameters, declarations, program_name = await self._resolve_template_jq(state, name)
        program = template.template_jq[program_name]
        if program.purpose != "update":
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} has purpose {program.purpose!r}; "
                f"apply needs an 'update'-purpose program (eval an 'input' one instead)"
            )
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
        body, prelude = await self._render_template_jq(template, program_name)
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
        ops = await self._resolve_template_jq_ops(state, subject, name, input_, conn=conn)
        applied = await self.apply(state, subject, ops, op_id=op_id, origin=origin, conn=conn)
        return TemplateJqApplyResult(
            name=name, applied=applied.applied, data=applied.data, seq=applied.seq, skipped=applied.skipped
        )
