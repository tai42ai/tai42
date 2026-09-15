"""The template lifecycle and the bounded template cache.

Stores and serves template documents, memoizing an inline-fragment template on
``(name, updated_at)`` while resolving and re-validating a by-id fragment fresh each time; a
save renders and compiles every by-id ``check`` / ``template_jq`` / ``reconcile`` body — the
point a stored resource can be fetched — and re-validates every live attach.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.states.errors import (
    StateNotFoundError,
    TemplateExistsError,
    TemplateInUseError,
    TemplateValidationError,
)
from tai42_contract.states.models import StateTemplateDocument
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.jq_util import compile_check
from tai42_kit.utils.render import resolve_schema_body

from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.rows import _SCHEMA_BODY_ADAPTER, _resolve_state_schema
from tai42_skeleton.states.templates import (
    DECLARATIONS_CHECK_VARIABLES,
    MEMBER_JQ_VARIABLES,
    StateTemplate,
    compose_effective_schema,
    template_jq_prelude,
    validate_template,
)
from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError, TemplateNotFoundError


class _TemplateMixin(_StatesServiceBase):
    async def _resolve_template_body(self, name: str, body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Resolve a stored template body's fragment ``schema`` to the plain fragment dict callers work on.

        The ``TemplatedText | dict`` union becomes the plain fragment dict
        :func:`validate_template` and every composition work on, returning
        ``(body_with_resolved_schema, was_by_id)``.

        An inline dict fragment is used as-is (``was_by_id`` False). A by-id (or inline
        templated) fragment is rendered and parsed to its schema (``was_by_id`` True); an
        unfetchable id, or a body that does not render to a JSON object, raises loudly naming the
        template. The stored body keeps the union — only this in-memory copy carries the resolved
        fragment.
        """
        typed = _SCHEMA_BODY_ADAPTER.validate_python(body.get("schema", {}))
        if not isinstance(typed, TemplatedText):
            return body, False
        fragment = await resolve_schema_body(f"template {name!r} schema", typed)
        return {**body, "schema": fragment}, True

    async def _validated_template(self, row: dict[str, Any]) -> StateTemplate:
        """Validate a template row into a :class:`StateTemplate`, its fragment ``schema`` union resolved first.

        An inline-fragment template is memoized on ``(name, updated_at)`` — an unchanged
        row is served from a bounded LRU; a by-id fragment is resolved and validated FRESH
        each time (never memoized), so it always tracks the stored resource it names.
        """
        resolved_body, by_id = await self._resolve_template_body(row["name"], row["body"])
        if by_id:
            return validate_template(resolved_body)
        key = (row["name"], row["updated_at"])
        cached = self._template_cache.get(key)
        if cached is not None:
            self._template_cache.move_to_end(key)
            return cached
        template = validate_template(resolved_body)
        self._template_cache[key] = template
        self._template_cache.move_to_end(key)
        if len(self._template_cache) > self._TEMPLATE_CACHE_MAX:
            self._template_cache.popitem(last=False)
        return template

    async def list_templates(self) -> list[StateTemplateDocument]:
        self._ensure_available()
        return [StateTemplateDocument.model_validate(row["body"]) for row in await self._store.list_templates()]

    async def list_templates_catalog(self) -> list[dict[str, Any]]:
        """The template-catalog projection the ``GET /api/state-templates`` list serves.

        Each stored document plus ``attached_to`` (the number of states the template is
        attached on) and ``shipped_default`` (true when the template carries a seed
        ``shipped_hash`` — an unedited shipped default). The attach counts are one query
        over every template.
        """
        self._ensure_available()
        counts = await self._store.attached_template_counts()
        catalog: list[dict[str, Any]] = []
        for row in await self._store.list_templates():
            document = StateTemplateDocument.model_validate(row["body"]).model_dump()
            document["attached_to"] = counts.get(row["name"], 0)
            document["shipped_default"] = row["shipped_hash"] is not None
            catalog.append(document)
        return catalog

    async def get_template(self, name: str) -> StateTemplateDocument | None:
        self._ensure_available()
        row = await self._store.get_template(name)
        if row is None:
            return None
        await self._validated_template(row)  # loud on a corrupt stored body
        return StateTemplateDocument.model_validate(row["body"])

    @staticmethod
    async def _compile_by_id_declarations_check(template: StateTemplate) -> None:
        """Render and compile a by-id declarations ``check`` at the save door.

        The save door is the point that can fetch the stored resource an inline compile in
        :func:`validate_template` cannot. An unfetchable id fails the save loudly, naming
        the field and the id; a rendered jq that does not compile is the same loud template
        error validate raises for inline jq.
        """
        if template.declarations is None:
            return
        check = template.declarations.check
        if check is None or check.id is None:
            return
        try:
            rendered = await tai42_app.storage.resource_manager.render_templated_text(check)
        except (TemplateNotFoundError, TemplateLocaleNotFoundError) as exc:
            raise TemplateValidationError(
                f"template {template.name!r} declarations check references stored id {check.id!r}, "
                f"which could not be fetched: {exc}"
            ) from exc
        try:
            compile_check(rendered, variables=DECLARATIONS_CHECK_VARIABLES)
        except Exception as exc:
            raise TemplateValidationError(
                f"template {template.name!r} declarations check is not a valid jq expression: {exc}"
            ) from exc

    async def _render_program_body_at_save(
        self, template: StateTemplate, program_name: str, text: TemplatedText
    ) -> str:
        """Render one ``template_jq`` program body at the save door.

        The save door is the point that can fetch a stored resource an inline compile in
        :func:`validate_template` cannot. An unfetchable id fails the save loudly naming
        the program and the id.
        """
        try:
            return await tai42_app.storage.resource_manager.render_templated_text(text)
        except (TemplateNotFoundError, TemplateLocaleNotFoundError) as exc:
            raise TemplateValidationError(
                f"template {template.name!r} template_jq {program_name!r} references stored id {text.id!r}, "
                f"which could not be fetched: {exc}"
            ) from exc

    async def _compile_by_id_template_jq(self, template: StateTemplate) -> None:
        """Render and compile a by-id ``template_jq`` program at the save door.

        When a program body is by-id, every program is rendered (the sibling prelude needs
        each input body) and compiled over the full prelude; an unfetchable id fails the
        save loudly naming the program and the id, and a rendered jq that does not compile
        is the same loud template error validate raises for inline jq. An all-inline
        section is already compiled by :func:`validate_template`, so this is a no-op for it.
        """
        programs = template.template_jq
        if not programs or all(p.jq.content is not None for p in programs.values()):
            return
        rendered = {name: await self._render_program_body_at_save(template, name, p.jq) for name, p in programs.items()}
        rendered_inputs = {name: rendered[name] for name, p in programs.items() if p.purpose == "input"}
        prelude = template_jq_prelude(rendered_inputs)
        for name, program in programs.items():
            variables = (*MEMBER_JQ_VARIABLES, "params") if program.purpose == "input" else MEMBER_JQ_VARIABLES
            try:
                compile_check(prelude + rendered[name], variables=variables)
            except Exception as exc:
                raise TemplateValidationError(
                    f"template {template.name!r} template_jq {name!r} is not a valid jq expression: {exc}"
                ) from exc

    async def _compile_by_id_reconcile(self, template: StateTemplate) -> None:
        """Render and compile a by-id ``reconcile`` program at the save door.

        The save door is the point that can fetch a stored resource an inline compile in
        :func:`validate_template` cannot. An unfetchable id fails the save loudly naming
        the program and the id; a rendered jq that does not compile is the same loud
        template error validate raises for inline jq. An inline program is already compiled
        by :func:`validate_template`.
        """
        if template.reconcile is None:
            return
        for label, text in (
            ("orphans", template.reconcile.orphans),
            ("close", template.reconcile.close),
            ("resolutions", template.reconcile.resolutions),
        ):
            if text.id is None:
                continue
            try:
                rendered = await tai42_app.storage.resource_manager.render_templated_text(text)
            except (TemplateNotFoundError, TemplateLocaleNotFoundError) as exc:
                raise TemplateValidationError(
                    f"template {template.name!r} reconcile {label} references stored id {text.id!r}, "
                    f"which could not be fetched: {exc}"
                ) from exc
            try:
                compile_check(rendered)
            except Exception as exc:
                raise TemplateValidationError(
                    f"template {template.name!r} reconcile {label} is not a valid jq expression: {exc}"
                ) from exc

    async def put_template(self, doc: StateTemplateDocument, *, replace: bool) -> StateTemplateDocument:
        """Store a template document, validating every live attach before the write.

        Runs every registered attach validator over each live attach before the write (a
        raise leaves the stored document untouched); overwriting an existing name without
        ``replace`` raises :class:`TemplateExistsError`.
        """
        self._ensure_available()
        # ``exclude_none`` drops an unset ``declarations`` (None) so the deep validator
        # sees the same absent-key shape ``to_document`` emits, never a null section.
        body = doc.model_dump(by_alias=True, exclude_none=True)
        # The fragment ``schema`` is the ``TemplatedText | dict`` union: resolve a by-id fragment
        # to its schema for structural validation (markers, regime/jq paths) at the save door,
        # exactly as a by-id declarations ``check`` is rendered here. An unfetchable id fails the
        # save loudly, naming the template. The UNION is stored as-is (``stored_body`` below), so
        # a read serves the by-id reference back.
        resolved_body, _by_id = await self._resolve_template_body(doc.name, body)
        template = validate_template(resolved_body)
        await self._compile_by_id_declarations_check(template)
        await self._compile_by_id_template_jq(template)
        await self._compile_by_id_reconcile(template)
        existing = await self._store.get_template(template.name)
        if existing is not None and not replace:
            raise TemplateExistsError(
                f"template {template.name!r} already exists — upload with replace=true to overwrite it"
            )
        # Store the canonical document but with the ORIGINAL union fragment (never the resolved
        # snapshot), so the stored body carries the by-id reference unchanged.
        stored_body = {**template.to_document(), "schema": body["schema"]}
        template_doc = StateTemplateDocument.model_validate(stored_body)
        attachment_rows = await self._store.list_attachments_of_template(template.name)
        for row in attachment_rows:
            attachment_declarations = dict(row["declarations"] or {})
            resolved = self._effective_parameters(template, dict(row["parameters"] or {}))
            try:
                await self._validate_attach_values(template, resolved, attachment_declarations)
                effective = compose_effective_schema(
                    await _resolve_state_schema(
                        f"state {row['state']!r} schema", (await self._require_declaration(row["state"]))["schema"]
                    ),
                    [
                        (m, p, pa)
                        for m, p, pa, _d in await self._load_state_attachments(
                            row["state"], override={template.name: template}
                        )
                    ],
                )
                await self._run_attach_validators(template_doc, attachment_declarations, effective)
            except TemplateValidationError as exc:
                raise TemplateInUseError(
                    f"template {template.name!r} cannot be replaced: its attach on state {row['state']!r} no longer "
                    f"validates: {exc}"
                ) from exc
        await self._store.upsert_template(template.name, stored_body, None)
        for row in attachment_rows:
            resolved = self._effective_parameters(template, dict(row["parameters"] or {}))
            base_schema = (await self._require_declaration(row["state"]))["schema"]
            effective = await self._compose_effective(row["state"], base_schema)
            await self._store.update_attachment_parameters(
                row["state"], template.name, resolved, effective_schema=effective
            )
        return template_doc

    async def delete_template(self, name: str) -> None:
        """Delete a template document; refused while it is attached."""
        self._ensure_available()
        if await self._store.get_template(name) is None:
            raise StateNotFoundError(f"no template {name!r}")
        attachments = await self._store.list_attachments_of_template(name)
        if attachments:
            states = ", ".join(sorted(m["state"] for m in attachments))
            raise TemplateInUseError(f"template {name!r} is attached on state(s) {states} — detach it first")
        await self._store.delete_template(name)
