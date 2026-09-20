"""The ``app.states`` facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from tai42_contract.states import (
        ApplyResult,
        AttachBody,
        AttachReconciler,
        AttachValidator,
        ConsumerLister,
        ConsumerRow,
        StateContext,
        StateDeclaration,
        StateRecord,
        StateSubject,
        StateTemplateDocument,
        TemplateJqApplyResult,
        TemplateJqResult,
        WriteOrigin,
        WritesPage,
    )


class StatesFacet(_Facet):
    """``app.states`` — the subject-keyed state store namespace (``AppStates``).

    The door-agnostic record substrate every door and tool reads and writes a subject's document
    through, plus the template/attach lifecycle and the consumer/seed/attach-validator seams.
    Forwards to the app's shared :class:`~tai42_skeleton.states.service.StatesService` and its
    registries; the write chokepoint completes provenance and the gate refuses 501 while the
    store is unbound.
    """

    # -- declarations --
    async def list_declarations(self) -> list[StateDeclaration]:
        """List every registered state declaration."""
        return await self._app._states_service.list_declarations()

    async def get_declaration(self, name: str) -> StateDeclaration | None:
        """The state declaration named ``name``, or ``None`` when none is registered."""
        return await self._app._states_service.get_declaration(name)

    async def served_declaration(self, name: str) -> dict[str, Any]:
        """The composed declaration read the ``GET /api/states/{name}`` route serves.

        Carries base ``schema``, composed ``effective_schema``, ``subject_kinds``,
        ``default_subject_kind``, the state's ``attachments`` and computed ``regimes``.
        Skeleton-only (the HTTP composed view), so it is off the ``AppStates`` protocol — a
        consumer reads the typed :meth:`get_declaration` + :meth:`list_attachments` — the
        ``target_validator`` precedent.
        """
        return await self._app._states_service.served_declaration(name)

    async def put_declaration(self, decl: StateDeclaration) -> StateDeclaration:
        """Register or replace ``decl`` and return the stored declaration."""
        return await self._app._states_service.put_declaration(decl)

    async def delete_declaration(self, name: str) -> None:
        """Remove the state declaration named ``name``."""
        return await self._app._states_service.delete_declaration(name)

    async def stats(self, name: str) -> dict[str, Any]:
        """Record counts and storage stats for the state named ``name``."""
        return await self._app._states_service.stats(name)

    # -- templates --
    async def list_templates(self) -> list[StateTemplateDocument]:
        """List every stored state-template document."""
        return await self._app._states_service.list_templates()

    async def list_templates_catalog(self) -> list[dict[str, Any]]:
        """The template-catalog projection the ``GET /api/state-templates`` list route serves.

        Each stored document plus ``attached_to`` (the number of states it is attached on) and
        ``shipped_default`` (whether it is an unedited shipped default). Skeleton-only (the HTTP
        catalog view), so it is off the ``AppStates`` protocol — a consumer reads the typed
        :meth:`list_templates` — the ``served_declaration`` precedent.
        """
        return await self._app._states_service.list_templates_catalog()

    async def get_template(self, name: str) -> StateTemplateDocument | None:
        """The state-template document named ``name``, or ``None`` when absent."""
        return await self._app._states_service.get_template(name)

    async def put_template(self, doc: StateTemplateDocument, *, replace: bool) -> StateTemplateDocument:
        """Store ``doc`` and return it; ``replace`` overwrites an existing template of the same name."""
        return await self._app._states_service.put_template(doc, replace=replace)

    async def delete_template(self, name: str) -> None:
        """Remove the state-template document named ``name``."""
        return await self._app._states_service.delete_template(name)

    # -- attachments --
    async def list_attachments(self, state: str | None = None, *, template: str | None = None) -> list[dict[str, Any]]:
        """List template attachments, optionally filtered by ``state`` and/or ``template``."""
        return await self._app._states_service.list_attachments(state, template=template)

    async def attach(self, state: str, template: str, body: AttachBody, *, skip_reconcilers: bool = False) -> None:
        """Attach ``template`` to ``state`` with ``body``; ``skip_reconcilers`` bypasses reconcilers."""
        return await self._app._states_service.attach(state, template, body, skip_reconcilers=skip_reconcilers)

    async def update_attachment_declarations(
        self,
        state: str,
        template: str,
        declarations: dict[str, Any],
        *,
        options: dict[str, Any] | None = None,
        skip_reconcilers: bool = False,
    ) -> None:
        """Update the per-template ``declarations`` of ``template``'s attachment on ``state``."""
        return await self._app._states_service.update_attachment_declarations(
            state, template, declarations, options=options, skip_reconcilers=skip_reconcilers
        )

    async def detach(self, state: str, template: str) -> None:
        """Remove ``template`` from ``state``."""
        return await self._app._states_service.detach(state, template)

    # -- backup restore --
    async def restore_aliases(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        """The backup section's alias-restore path; off the ``AppStates`` protocol.

        The ``restore_records`` precedent.
        """
        return await self._app._states_service.restore_aliases(state, rows, origin=origin)

    async def restore_records(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        """The backup section's record-restore path; off the ``AppStates`` protocol.

        A consumer writes records through :meth:`replace` / :meth:`merge` / :meth:`apply` — the
        ``served_declaration`` precedent.
        """
        return await self._app._states_service.restore_records(state, rows, origin=origin)

    # -- records --
    async def read(self, state: str, subject: StateSubject) -> StateRecord | None:
        """The stored record for ``subject`` under ``state``, or ``None`` when absent."""
        return await self._app._states_service.read(state, subject)

    async def replace(
        self, state: str, subject: StateSubject, data: dict[str, Any], *, origin: WriteOrigin
    ) -> StateRecord:
        """Replace ``subject``'s document under ``state`` with ``data`` and return the new record."""
        return await self._app._states_service.replace(state, subject, data, origin=origin)

    async def merge(
        self, state: str, subject: StateSubject, patch: dict[str, Any], *, origin: WriteOrigin
    ) -> StateRecord:
        """Merge ``patch`` into ``subject``'s document under ``state`` and return the new record."""
        return await self._app._states_service.merge(state, subject, patch, origin=origin)

    async def apply(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: WriteOrigin,
    ) -> ApplyResult:
        """Apply ``ops`` to ``subject``'s document under ``state``; ``op_id`` makes the write idempotent."""
        return await self._app._states_service.apply(state, subject, ops, op_id=op_id, origin=origin)

    async def eval_template_jq(
        self, state: str, subject: StateSubject, name: str, params: dict[str, Any]
    ) -> TemplateJqResult:
        """Evaluate template ``name``'s jq program against ``subject`` under ``state`` without writing."""
        return await self._app._states_service.eval_template_jq(state, subject, name, params)

    async def apply_template_jq(
        self,
        state: str,
        subject: StateSubject,
        name: str,
        input_: Any,
        *,
        op_id: str | None,
        origin: WriteOrigin,
    ) -> TemplateJqApplyResult:
        """Apply template ``name``'s jq program to ``subject`` under ``state`` and persist the result."""
        return await self._app._states_service.apply_template_jq(
            state, subject, name, input_, op_id=op_id, origin=origin
        )

    async def erase(self, state: str, subject: StateSubject, *, origin: WriteOrigin) -> None:
        """Erase ``subject``'s document under ``state``."""
        return await self._app._states_service.erase(state, subject, origin=origin)

    async def fold(
        self, state: str, subject: StateSubject, into: StateSubject, mode: str, *, origin: WriteOrigin
    ) -> dict[str, Any]:
        """Fold ``subject`` into ``into`` under ``state`` using ``mode`` and return the fold outcome."""
        return await self._app._states_service.fold(state, subject, into, mode, origin=origin)

    async def list_subjects(
        self, state: str, *, kind: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """Page the subjects holding a document under ``state``, optionally filtered by ``kind``."""
        return await self._app._states_service.list_subjects(state, kind=kind, limit=limit, cursor=cursor)

    async def search(
        self, state: str, filters: dict[str, Any], *, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """Page the documents under ``state`` matching ``filters``."""
        return await self._app._states_service.search(state, filters, limit=limit, cursor=cursor)

    async def writes(
        self, state: str, subject: StateSubject, *, limit: int | None = None, cursor: str | None = None
    ) -> WritesPage:
        """Page the write history for ``subject`` under ``state``."""
        return await self._app._states_service.writes(state, subject, limit=limit, cursor=cursor)

    async def prune_expired(self) -> dict[str, int]:
        """Delete every expired record and return per-state removal counts."""
        return await self._app._states_service.prune_expired()

    # -- context --
    def context(self) -> StateContext | None:
        """The active state context, or ``None`` when none is bound."""
        return self._app._states_service.context()

    # -- consumers --
    def register_consumer_lister(self, kind: str, lister: ConsumerLister) -> None:
        """Register ``lister`` as the consumer source for subjects of ``kind``."""
        return self._app._states_service.register_consumer_lister(kind, lister)

    async def consumers(self, state: str) -> list[ConsumerRow]:
        """List the consumers referencing the state named ``state``."""
        return await self._app._states_service.consumers(state)

    # -- seeds --
    def register_template_seed(self, doc: StateTemplateDocument) -> None:
        """Register ``doc`` as a shipped template seed."""
        return self._app._states_service.register_template_seed(doc)

    # -- attach validation --
    def register_attach_validator(self, validator: AttachValidator) -> None:
        """Register ``validator`` to gate future attachments."""
        return self._app._states_service.register_attach_validator(validator)

    def register_attach_reconciler(self, reconciler: AttachReconciler) -> None:
        """Register ``reconciler`` to run when an attachment changes."""
        return self._app._states_service.register_attach_reconciler(reconciler)
