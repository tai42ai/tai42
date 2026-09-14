"""The attach lifecycle, subject validation, effective-schema composition, and attach-value
validation.

Attaching or re-declaring a template validates its path/parameters/declarations, runs every
registered attach validator over the composed effective schema, and recomposes the state's
effective schema; subject validation refuses a subject a state's declaration does not admit.
"""

from __future__ import annotations

from typing import Any

import jsonschema
from jsonschema import Draft202012Validator
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import (
    AttachConflictError,
    StateNotFoundError,
    SubjectRefusedError,
    TemplateValidationError,
)
from tai42_contract.states.models import (
    PERSON_KIND,
    AttachBody,
    StateDeclaration,
    StateSubject,
    StateTemplateDocument,
)
from tai42_kit.utils.data.jq_util import run_jq_first

from tai42_skeleton.states.schema import _validate_schema
from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.rows import _resolve_state_schema, _row_to_declaration
from tai42_skeleton.states.templates import StateTemplate, compose_effective_schema, regime_for


class _AttachmentMixin(_StatesServiceBase):
    async def validate_subject(self, decl: StateDeclaration, subject: StateSubject) -> None:
        """Refuse a subject that a state's declaration does not admit: an undeclared
        kind, or — for kind ``person`` — an unknown person or a person whose target does
        not match the subject's. The ``ConversationPersonStore`` is constructed LAZILY and
        ONLY on the ``person`` branch (its constructor raises 501 without the redis
        conversations backend), so no state of another kind is gated on redis."""
        if subject.kind not in decl.subject_kinds:
            raise SubjectRefusedError(
                f"subject kind {subject.kind!r} is not declared by state {decl.name!r} "
                f"(declared kinds: {sorted(decl.subject_kinds)})"
            )
        if subject.kind != PERSON_KIND:
            return
        from tai42_skeleton.conversations.persons import ConversationPersonStore
        from tai42_skeleton.conversations.settings import ConversationsSettings

        person = await ConversationPersonStore(ConversationsSettings()).get_by_id(subject.key)
        if person is None:
            raise SubjectRefusedError(
                f"subject key {subject.key!r} of kind 'person' names no person in the identity store"
            )
        if person.target_kind != subject.target_kind or person.target_name != subject.target_name:
            raise SubjectRefusedError(
                f"person {subject.key!r} belongs to target {person.target_kind}/{person.target_name}, "
                f"not the subject's {subject.target_kind}/{subject.target_name}"
            )

    async def list_attachments(self, state: str | None = None, *, template: str | None = None) -> list[dict[str, Any]]:
        self._ensure_available()
        if state is not None and template is not None:
            row = await self._store.get_attachment(state, template)
            rows = [] if row is None else [row]
        elif state is not None:
            rows = await self._store.list_attachments_for_state(state)
        elif template is not None:
            rows = await self._store.list_attachments_of_template(template)
        else:
            rows = await self._store.list_all_attachments()
        return [
            {
                "state": r["state"],
                "template": r["template"],
                "path": list(r["path"]),
                "parameters": dict(r["parameters"] or {}),
                "declarations": dict(r["declarations"] or {}),
            }
            for r in rows
        ]

    async def attach(self, state: str, template_name: str, body: AttachBody, *, skip_reconcilers: bool = False) -> None:
        """Attach a template on a state: validate path/parameters/declarations (+ check), run
        every registered attach validator over the composed effective schema, run every
        registered reconciler, then store the resolved parameters and the recomposed
        effective schema in one transaction (nothing derived is materialized). The
        reconcilers and the attach write share ONE transaction, so a reconciler's record
        writes commit with the attach or roll back together with a refusal. ``body.options``
        is a per-operation directive passed to the reconcilers, never stored.
        ``skip_reconcilers`` (backup restore only) runs the validators but not the
        reconcilers — a restored attachment is a snapshot, not a re-attach."""
        self._ensure_available()
        path = list(body.path)
        parameters = dict(body.parameters or {})
        declarations = dict(body.declarations or {})
        options = dict(body.options or {})
        decl = await self._require_declaration(state)
        template = await self._get_template_or_raise(template_name)
        self._validate_attach_path(path)
        if await self._store.get_attachment(state, template_name) is not None:
            raise AttachConflictError(
                f"template {template_name!r} is already attached on state {state!r} — detach it to change "
                f"path/parameters"
            )
        await self._validate_attach_values(template, parameters, declarations)
        resolved = self._effective_parameters(template, parameters)
        existing = await self._load_state_attachments(state)
        effective = compose_effective_schema(
            await _resolve_state_schema(f"state {state!r} schema", decl["schema"]),
            [*[(m, p, pa) for m, p, pa, _d in existing], (template, list(path), resolved)],
        )
        _validate_schema(effective)
        template_doc = StateTemplateDocument.model_validate(template.to_document())
        await self._run_attach_validators(template_doc, declarations, effective)
        reconcilers = [] if skip_reconcilers else self._attach_reconcilers.all()
        if reconcilers:
            async with self._store.begin() as conn:
                await self._run_attach_reconcilers(
                    reconcilers,
                    state,
                    template_doc,
                    "attach",
                    previous_declarations=None,
                    new_declarations=declarations,
                    options=options,
                    conn=conn,
                )
                await self._store.upsert_attachment(
                    state, template_name, path, resolved, declarations, effective_schema=effective, conn=conn
                )
        else:
            await self._store.upsert_attachment(
                state, template_name, path, resolved, declarations, effective_schema=effective
            )

    async def update_attachment_declarations(
        self,
        state: str,
        template_name: str,
        declarations: dict[str, Any],
        *,
        options: dict[str, Any] | None = None,
        skip_reconcilers: bool = False,
    ) -> None:
        """Rewrite an attachment's declarations, re-running every registered attach validator and
        reconciler and recomposing the effective schema before the write. The reconcilers
        and the write share ONE transaction. ``options`` is a per-operation directive
        passed to the reconcilers, never stored. ``skip_reconcilers`` (backup restore only)
        runs the validators but not the reconcilers."""
        self._ensure_available()
        declarations = dict(declarations or {})
        options = dict(options or {})
        row = await self._store.get_attachment(state, template_name)
        if row is None:
            raise StateNotFoundError(f"template {template_name!r} is not attached on state {state!r}")
        template = await self._get_template_or_raise(template_name)
        await self._validate_attach_values(template, dict(row["parameters"] or {}), declarations)
        effective = await self._compose_effective(state, (await self._require_declaration(state))["schema"])
        template_doc = StateTemplateDocument.model_validate(template.to_document())
        await self._run_attach_validators(template_doc, declarations, effective)
        reconcilers = [] if skip_reconcilers else self._attach_reconcilers.all()
        if reconcilers:
            async with self._store.begin() as conn:
                await self._run_attach_reconcilers(
                    reconcilers,
                    state,
                    template_doc,
                    "update_declarations",
                    previous_declarations=dict(row["declarations"] or {}),
                    new_declarations=declarations,
                    options=options,
                    conn=conn,
                )
                await self._store.update_attachment_declarations(
                    state, template_name, declarations, effective_schema=effective, conn=conn
                )
        else:
            await self._store.update_attachment_declarations(
                state, template_name, declarations, effective_schema=effective
            )

    async def detach(self, state: str, template_name: str) -> None:
        """Remove an attachment and recompose the state's effective schema (nothing else)."""
        self._ensure_available()
        if await self._store.get_attachment(state, template_name) is None:
            raise StateNotFoundError(f"template {template_name!r} is not attached on state {state!r}")
        decl = await self._require_declaration(state)
        remaining = [
            (m, p, pa) for m, p, pa, _d in await self._load_state_attachments(state) if m.name != template_name
        ]
        effective = compose_effective_schema(
            await _resolve_state_schema(f"state {state!r} schema", decl["schema"]), remaining
        )
        await self._store.delete_attachment(state, template_name, effective_schema=effective)

    async def effective_schema_for(self, state: str) -> dict[str, Any]:
        """The stored effective schema (base + every attachment's fragment) for a declared
        state — the schema every document validation reads."""
        self._ensure_available()
        return (await self._require_declaration(state))["effective_schema"]

    async def served_declaration(self, name: str) -> dict[str, Any]:
        """The full served declaration read: ``schema`` (base), ``effective_schema``,
        ``subject_kinds``, ``default_subject_kind``, ``retention_days`` (``None`` when the
        state keeps records forever), ``attachments[]``, ``regimes[]`` (the absolute regime paths
        every attachment declares) and ``updated_at`` (the ISO timestamp of the last write) — the
        one read a consumer's bind-time checks and the Studio's fields view consume. Carries
        the same fields the list read dumps, so an edit form round-trips a declaration
        (``retention_days`` included) without dropping any."""
        self._ensure_available()
        decl = await self._require_declaration(name)
        attachments = await self._load_state_attachments(name)
        regimes = self._compose_regimes(attachments)
        # Serialize ``updated_at`` through the same model dump the list read uses, so both
        # reads render the timestamp identically (pydantic's ISO ``…Z``), never two formats.
        updated_at = _row_to_declaration(decl).model_dump(mode="json")["updated_at"]
        return {
            "name": decl["name"],
            "description": decl.get("description") or "",
            "schema": decl["schema"],
            "effective_schema": decl["effective_schema"],
            "subject_kinds": list(decl["subject_kinds"]),
            "default_subject_kind": decl["default_subject_kind"],
            "retention_days": decl["retention_days"],
            "attachments": [
                {"template": m.name, "path": list(p), "parameters": dict(pa), "declarations": dict(d)}
                for m, p, pa, d in attachments
            ],
            "regimes": regimes,
            "updated_at": updated_at,
        }

    def regime_for_path(self, template: StateTemplate, relative_path: list[Any]) -> str:
        """The regime governing ``relative_path`` in ``template`` — exposed for a consumer's
        bind-time single-writer check."""
        return regime_for(template, relative_path)

    async def _require_declaration(self, state: str) -> dict[str, Any]:
        decl = await self._store.get_declaration(state)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        return decl

    async def _require_declaration_decl(self, state: str) -> StateDeclaration:
        return _row_to_declaration(await self._require_declaration(state))

    async def _get_template_or_raise(self, name: str) -> StateTemplate:
        row = await self._store.get_template(name)
        if row is None:
            raise StateNotFoundError(f"no template {name!r}")
        return await self._validated_template(row)

    async def _load_state_attachments(
        self, state: str, *, override: dict[str, StateTemplate] | None = None
    ) -> list[tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any]]]:
        """Every attachment on the state as ``(template, path, parameters, declarations)``.
        ``override`` supplies a not-yet-stored template body (a template replace composes
        against the candidate)."""
        override = override or {}
        out: list[tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any]]] = []
        for row in await self._store.list_attachments_for_state(state):
            template = override.get(row["template"]) or await self._get_template_or_raise(row["template"])
            out.append((template, list(row["path"]), dict(row["parameters"] or {}), dict(row["declarations"] or {})))
        return out

    async def _compose_effective(self, state: str, base_schema: Any) -> dict[str, Any]:
        """The effective schema for ``base_schema`` over the state's CURRENT attachments.

        ``base_schema`` is the ``TemplatedText | dict`` union (a by-id base resolves to its
        schema); composition is over the resolved schema."""
        resolved_base = await _resolve_state_schema(f"state {state!r} schema", base_schema)
        attachments = await self._load_state_attachments(state)
        return compose_effective_schema(resolved_base, [(m, p, pa) for m, p, pa, _d in attachments])

    @staticmethod
    def _compose_regimes(
        attachments: list[tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """The absolute write-regime rules over already-loaded ``attachments``: each attached
        template's regime paths prefixed by the attach path. The ONE composition every
        declaration read (``get_declaration``/``list_declarations``) and
        ``served_declaration`` share, so a served regime is identical across doors."""
        regimes: list[dict[str, Any]] = []
        for template, base_path, _params, _decls in attachments:
            for rule in template.regimes:
                regimes.append({"path": [*base_path, *rule.path], "regime": rule.regime})
        return regimes

    def _validate_attach_path(self, path: Any) -> None:
        if not isinstance(path, list):
            raise AttachConflictError("attach path must be a list of object keys")
        for seg in path:
            if not isinstance(seg, str) or not seg:
                raise AttachConflictError(
                    f"attach path segment {seg!r} must be a non-empty object key (an attach never sits on a list index)"
                )

    @staticmethod
    def _effective_parameters(template: StateTemplate, parameters: dict[str, Any]) -> dict[str, Any]:
        """The parameter map an attach PERSISTS: the template's defaults overlaid by the
        client's supplied values."""
        return {**template.defaults(), **dict(parameters or {})}

    async def _validate_attach_values(
        self, template: StateTemplate, parameters: dict[str, Any], declarations: dict[str, Any]
    ) -> None:
        """Validate an attach's effective parameter values against each parameter's schema
        (every no-default parameter supplied) and its declarations against the template's
        declarations schema and optional ``check`` predicate. Loud on the first failure.

        The ``check`` runs over the declarations with the attach's EFFECTIVE parameters
        (template defaults overlaid by supplied values — the map the runtime sees) bound as
        the named jq variable ``$parameters``, so a check may constrain a declaration
        against a parameter (e.g. against a parameter-declared enum) at the earliest point
        both are known."""
        effective = self._effective_parameters(template, parameters)
        _validate_effective_parameters(template, effective)
        await _validate_attach_declarations(template, declarations, effective)


def _validate_effective_parameters(template: StateTemplate, effective: dict[str, Any]) -> None:
    """Each effective parameter value against its parameter schema, and every no-default
    parameter supplied. Loud on the first failure."""
    for name, value in effective.items():
        param = template.parameters.get(name)
        if param is None:
            raise TemplateValidationError(f"attach supplies unknown parameter {name!r} for template {template.name!r}")
        try:
            Draft202012Validator(param.schema).validate(value)
        except jsonschema.ValidationError as exc:
            raise TemplateValidationError(f"attach parameter {name!r} is invalid: {exc.message}") from exc
    for name, param in template.parameters.items():
        if not param.has_default and name not in effective:
            raise TemplateValidationError(
                f"attach of template {template.name!r} must supply parameter {name!r} (it has no default)"
            )


async def _validate_attach_declarations(
    template: StateTemplate, declarations: dict[str, Any], effective: dict[str, Any]
) -> None:
    """The attach's declarations against the template's declarations schema and its optional
    ``check`` predicate (binding the effective parameters as ``$parameters``). Loud on the
    first failure."""
    if template.declarations is None:
        if declarations:
            raise TemplateValidationError(
                f"template {template.name!r} declares no declarations section, so none may be supplied"
            )
        return
    try:
        Draft202012Validator(template.declarations.schema_).validate(declarations)
    except jsonschema.ValidationError as exc:
        raise TemplateValidationError(
            f"attach declarations are invalid under template {template.name!r}: {exc.message}"
        ) from exc
    if template.declarations.check is not None:
        try:
            # Render the templated check to its jq program IMMEDIATELY before evaluating
            # it; a by-id text whose stored resource cannot be fetched raises here and is
            # surfaced as the loud check-evaluation failure.
            rendered = await tai42_app.storage.resource_manager.render_templated_text(template.declarations.check)
            result = await run_jq_first(rendered, declarations, variables={"parameters": effective})
        except Exception as exc:
            raise TemplateValidationError(
                f"template {template.name!r} declarations check failed to evaluate: {exc}"
            ) from exc
        if result is not True:
            message = result if isinstance(result, str) else "the declarations violate the template's check rule"
            raise TemplateValidationError(f"attach declarations rejected by template {template.name!r}: {message}")
