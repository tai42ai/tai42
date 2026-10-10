"""The template lifecycle, the version-keyed template entries and the rendered-template facet reads.

Stores and serves template documents. A template is validated once per stored ``version`` (an
inline fragment's validated template is held in the catalog snapshot; a by-id fragment is
resolved through the resource manager on use, so it follows the stored resource). A save renders
and compiles every by-id ``check`` / ``template_jq`` / ``reconcile`` body — the point a stored
resource can be fetched — re-validates every live attach, and writes the template with every
attached declaration's version bump in one transaction. The rendered template (every body as jq
text, the input programs ordered) is served from the rendered-template cache.
"""

from __future__ import annotations

import copy
import time
from typing import Any

from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import (
    StateNotFoundError,
    TemplateExistsError,
    TemplateInUseError,
    TemplateValidationError,
)
from tai42_contract.states.models import StateTemplateDocument
from tai42_contract.states.rendered import (
    RenderedAttachment,
    RenderedStateTemplate,
    RenderedTemplateDeclarations,
    RenderedTemplateJq,
)
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.jq_util import compile_check
from tai42_kit.utils.render import resolve_schema_body

from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.catalog import TemplateEntry
from tai42_skeleton.states.service.rendered import RenderedEntry, rendered_digest
from tai42_skeleton.states.service.rows import _SCHEMA_BODY_ADAPTER, _resolve_state_schema
from tai42_skeleton.states.templates import (
    DECLARATIONS_CHECK_VARIABLES,
    MEMBER_JQ_VARIABLES,
    RECONCILE_JQ_VARIABLES,
    StateTemplate,
    compose_effective_schema,
    input_order,
    sibling_prelude,
    template_jq_prelude,
    validate_template,
)
from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError, TemplateNotFoundError
from tai42_skeleton.template.settings import template_cache_settings

#: The ``version`` a rendered candidate (a document not stored) carries.
CANDIDATE_VERSION = "candidate"


def _document(body: dict[str, Any]) -> StateTemplateDocument:
    """A stored template body as its typed document; a body the model refuses is a loud template error."""
    try:
        return StateTemplateDocument.model_validate(body)
    except ValidationError as exc:
        raise TemplateValidationError(str(exc)) from exc


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

    async def _template_at(self, name: str, version: int, body: dict[str, Any]) -> StateTemplate:
        """The validated template ``name`` stored at ``version`` with ``body``.

        An inline-fragment template is validated once per version and held in the catalog
        snapshot; a by-id fragment is resolved and validated on every call, so it always tracks
        the stored resource it names. A body that does not validate raises loudly.
        """
        template, _entry = await self._validated_entry(name, version, body)
        return template

    async def _validated_entry(
        self, name: str, version: int, body: dict[str, Any]
    ) -> tuple[StateTemplate, TemplateEntry]:
        """The validated template ``name`` at ``version`` and the entry built for that version.

        The entry returned is the one for ``version`` even when a concurrent reader has since
        inserted a higher version, so the caller serves exactly the version it read.
        """
        cached = self._catalog.template_at(name, version)
        if cached is not None and cached.template is not None:
            return cached.template, cached
        resolved_body, by_id = await self._resolve_template_body(name, body)
        template = validate_template(resolved_body)
        entry = self._catalog.insert_template(
            name, TemplateEntry(version=version, body=body, template=None if by_id else template)
        )
        return template, entry

    async def _template_entry(self, name: str) -> TemplateEntry | None:
        """The template entry at the stored version (one ``SELECT version`` probe), or ``None`` when absent.

        On a miss the row is read and its entry served at the version that read returned, even when a
        concurrent reader in this process has since inserted a newer one.
        """
        version = await self._store.template_version(name)
        if version is None:
            self._catalog.drop_template(name)
            return None
        cached = self._catalog.template_at(name, version)
        if cached is not None:
            return cached
        row = await self._store.get_template(name)
        if row is None:
            self._catalog.drop_template(name)
            return None
        _template, entry = await self._validated_entry(name, int(row["version"]), row["body"])
        return entry

    async def _get_template_or_raise(self, name: str) -> StateTemplate:
        entry = await self._template_entry(name)
        if entry is None:
            raise StateNotFoundError(f"no template {name!r}")
        return await self._template_at(name, entry.version, entry.body)

    async def list_templates(self) -> list[StateTemplateDocument]:
        self._ensure_available()
        return [_document(row["body"]) for row in await self._store.list_templates()]

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
            document = _document(row["body"]).model_dump()
            document["attached_to"] = counts.get(row["name"], 0)
            document["shipped_default"] = row["shipped_hash"] is not None
            catalog.append(document)
        return catalog

    async def get_template(self, name: str) -> StateTemplateDocument | None:
        self._ensure_available()
        entry = await self._template_entry(name)
        if entry is None:
            return None
        await self._template_at(name, entry.version, entry.body)  # loud on a corrupt stored body
        return _document(entry.body)

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
            extra = "params" if program.purpose == "input" else "input"
            try:
                compile_check(prelude + rendered[name], variables=(*MEMBER_JQ_VARIABLES, extra))
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
                compile_check(rendered, variables=RECONCILE_JQ_VARIABLES[label])
            except Exception as exc:
                raise TemplateValidationError(
                    f"template {template.name!r} reconcile {label} is not a valid jq expression: {exc}"
                ) from exc

    async def _validated_template_write(self, doc: StateTemplateDocument) -> tuple[StateTemplate, dict[str, Any]]:
        """Validate a template document for storing: ``(template, stored_body)``.

        The fragment ``schema`` is the ``TemplatedText | dict`` union: a by-id fragment is resolved
        for structural validation and every by-id ``check`` / ``template_jq`` / ``reconcile`` body is
        rendered and compiled here, the point a stored resource can be fetched (an unfetchable id
        fails loudly, naming the template). The stored body is the canonical document carrying the
        ORIGINAL union fragment, so a read serves a by-id reference back unchanged.
        """
        body = doc.model_dump(by_alias=True)
        resolved_body, _by_id = await self._resolve_template_body(doc.name, body)
        template = validate_template(resolved_body)
        await self._compile_by_id_declarations_check(template)
        await self._compile_by_id_template_jq(template)
        await self._compile_by_id_reconcile(template)
        return template, {**template.to_document(), "schema": body["schema"]}

    async def put_template(self, doc: StateTemplateDocument, *, replace: bool) -> StateTemplateDocument:
        """Store a template document, validating every live attach before the write.

        Runs every registered attach validator over each live attach before the write (a
        raise leaves the stored document untouched); overwriting an existing name without
        ``replace`` raises :class:`TemplateExistsError`.
        """
        self._ensure_available()
        template, stored_body = await self._validated_template_write(doc)
        existing = await self._store.get_template(template.name)
        if existing is not None and not replace:
            raise TemplateExistsError(
                f"template {template.name!r} already exists — upload with replace=true to overwrite it"
            )
        template_doc = StateTemplateDocument.model_validate(stored_body)
        attachment_rows = await self._store.list_attachments_of_template(template.name)
        rewrites: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
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
                await self._run_attach_validators(row["state"], template_doc, attachment_declarations, effective)
            except TemplateValidationError as exc:
                raise TemplateInUseError(
                    f"template {template.name!r} cannot be replaced: its attach on state {row['state']!r} no longer "
                    f"validates: {exc}"
                ) from exc
            rewrites.append((row["state"], resolved, effective))
        rewrites.sort(key=lambda item: item[0])
        # One transaction: every attached declaration is locked (sorted order) before the template
        # row changes, and each attachment rewrite bumps its declaration's version, so no reader
        # sees the new template with an attached declaration's old version.
        async with self._store.begin() as conn:
            await self._store.lock_declarations([state for state, _r, _e in rewrites], conn=conn)
            await self._store.upsert_template(template.name, stored_body, None, conn=conn)
            for state, resolved, effective in rewrites:
                await self._store.update_attachment_parameters(
                    state, template.name, resolved, effective_schema=effective, conn=conn
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

    # -- rendered templates ----------------------------------------------------
    async def _render_body(self, template: StateTemplate, where: str, text: TemplatedText) -> str:
        try:
            return await tai42_app.storage.resource_manager.render_templated_text(text)
        except (TemplateNotFoundError, TemplateLocaleNotFoundError) as exc:
            raise TemplateValidationError(
                f"template {template.name!r} {where} references stored id {text.id!r}, "
                f"which could not be fetched: {exc}"
            ) from exc

    async def _render_entry(self, template: StateTemplate, version_prefix: str | None) -> RenderedEntry:
        """Render every program body and the declarations check of ``template`` and order the input programs.

        A stored template's served ``version`` is ``"<version_prefix>:<digest>"``, the digest taken
        over everything this render produced; a candidate (``version_prefix`` ``None``) carries
        :data:`CANDIDATE_VERSION`.
        """
        bodies = {
            name: await self._render_body(template, f"template_jq {name!r}", program.jq)
            for name, program in template.template_jq.items()
        }
        inputs = {name: bodies[name] for name, program in template.template_jq.items() if program.purpose == "input"}
        order = input_order(inputs)
        check: str | None = None
        if template.declarations is not None and template.declarations.check is not None:
            check = await self._render_body(template, "declarations check", template.declarations.check)
        served = RenderedStateTemplate(
            name=template.name,
            description=template.description,
            version=CANDIDATE_VERSION,
            parameters=dict(template.parameters),
            schema=template.schema,
            regimes=list(template.regimes),
            declarations=(
                None
                if template.declarations is None
                else RenderedTemplateDeclarations(schema=template.declarations.schema_, check=check)
            ),
            trace=template.trace,
            template_jq={
                name: RenderedTemplateJq(
                    purpose=program.purpose,
                    jq=bodies[name],
                    params=list(program.params),
                    reads=[list(p) for p in program.reads],
                    writes=[list(p) for p in program.writes],
                )
                for name, program in template.template_jq.items()
            },
            input_order=order,
        )
        if version_prefix is not None:
            served = served.model_copy(update={"version": f"{version_prefix}:{rendered_digest(served)}"})
        return RenderedEntry(
            template=template,
            fragment=template.schema,
            bodies=bodies,
            declarations_check=check,
            input_order=tuple(order),
            sibling_prelude=sibling_prelude(inputs, order),
            served=served,
            cached_at=time.monotonic(),
        )

    async def _rendered(self, template: StateTemplate, version: int) -> RenderedEntry:
        """``template`` (stored at ``version``) rendered through the current resource manager.

        Served from the rendered-template cache keyed ``(name, version, manager epoch, manager
        generation)`` while fresh; nothing is cached while the manager's cache is off. The served
        token ``"<version>:<epoch>.<generation>:<digest>"`` changes whenever the key does and
        whenever a re-render produces different text.
        """
        manager = tai42_app.storage.resource_manager
        epoch, generation = manager.epoch, manager.generation
        prefix = f"{version}:{epoch}.{generation}"
        if not manager.cache_enabled:
            return await self._render_entry(template, prefix)
        key = (template.name, version, epoch, generation)
        cached = self._rendered_cache.get(key, template_cache_settings().ttl)
        if cached is not None:
            return cached
        entry = await self._render_entry(template, prefix)
        self._rendered_cache.put(key, entry)
        return entry

    async def get_rendered_template(self, name: str) -> RenderedStateTemplate | None:
        """The stored template ``name`` rendered — every body as jq text, the input programs ordered."""
        self._ensure_available()
        entry = await self._template_entry(name)
        if entry is None:
            return None
        template = await self._template_at(name, entry.version, entry.body)
        return (await self._rendered(template, entry.version)).served

    async def rendered_attachments(self, state: str) -> list[RenderedAttachment]:
        """Every attachment on ``state`` with its rendered template, ordered by template name; loud when undeclared."""
        self._ensure_available()
        entry = await self._catalog.catalog_entry(state)
        if entry is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        out: list[RenderedAttachment] = []
        for a in entry.attachments:
            template = await self._template_at(a.template, a.template_version, a.body)
            rendered = await self._rendered(template, a.template_version)
            out.append(
                RenderedAttachment(
                    state=state,
                    template=rendered.served,
                    path=list(a.path),
                    parameters=copy.deepcopy(a.parameters),
                    declarations=copy.deepcopy(a.declarations),
                    version=f"{entry.version}:{rendered.served.version}",
                )
            )
        return out

    async def render_template(self, doc: StateTemplateDocument) -> RenderedStateTemplate:
        """Render a candidate document that is not stored, validated as a store would (never cached)."""
        self._ensure_available()
        template, _stored_body = await self._validated_template_write(doc)
        return (await self._render_entry(template, None)).served
