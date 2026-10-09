"""The ``app.states`` facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_contract.states import ResolvedTemplateJq

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager
    from typing import Any, Literal

    from tai42_contract.states import (
        ApplyResult,
        AttachBody,
        AttachReconciler,
        AttachValidator,
        ConsumerLister,
        ConsumerRow,
        HeldPendingSave,
        PruneResult,
        RenderedAttachment,
        RenderedStateTemplate,
        StateBatchWrite,
        StateContext,
        StateDeclaration,
        StateDeclarationSaved,
        StateRecord,
        StateSubject,
        StateTemplateDocument,
        StateUnit,
        TemplateJqApplyResult,
        TemplateJqResult,
        UnitCommitResult,
        WriteOrigin,
        WritesPage,
    )

    from tai42_skeleton.states.outbox.models import OutboxRow
    from tai42_skeleton.states.service.pending_saves import RetryOutcome


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

    async def put_declaration(self, decl: StateDeclaration) -> StateDeclarationSaved:
        """Register or replace ``decl``; the stored declaration and the held saves it was accepted beside."""
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

    async def get_rendered_template(self, name: str) -> RenderedStateTemplate | None:
        """The stored template ``name`` rendered (every body as jq text, input programs ordered), or ``None``."""
        return await self._app._states_service.get_rendered_template(name)

    async def rendered_attachments(self, state: str) -> list[RenderedAttachment]:
        """Every attachment on ``state`` with its rendered template; refuses an undeclared state."""
        return await self._app._states_service.rendered_attachments(state)

    async def render_template(self, doc: StateTemplateDocument) -> RenderedStateTemplate:
        """Render a candidate template document that is not stored (``version`` ``"candidate"``)."""
        return await self._app._states_service.render_template(doc)

    # -- resolution --
    async def resolve_template_jq(
        self,
        state: str,
        name: str,
        *,
        purpose: Literal["input", "update"],
        declared: Collection[str] = (),
    ) -> ResolvedTemplateJq:
        """Resolve the ``template_jq`` reference ``name`` on ``state`` for a ``purpose`` call.

        ``declared`` templates not attached yet take part as if attached at ``[<template>]``.
        """
        (
            template,
            _version,
            path,
            _parameters,
            _declarations,
            program,
        ) = await self._app._states_service.resolve_template_jq(state, name, purpose=purpose, declared=declared)
        return ResolvedTemplateJq(
            template=template.name,
            program=program,
            purpose=purpose,
            params=list(template.template_jq[program].params),
            path=path,
        )

    async def resolve_subject(self, state: str, ref: str | Mapping[str, Any] | None) -> StateSubject:
        """Resolve the subject reference ``ref`` for ``state``: a full mapping, ``{kind, key}``, a key or ``None``."""
        return await self._app._states_service.resolve_subject(state, ref)

    # -- attachments --
    async def list_attachments(self, state: str | None = None, *, template: str | None = None) -> list[dict[str, Any]]:
        """List template attachments, optionally filtered by ``state`` and/or ``template``."""
        return await self._app._states_service.list_attachments(state, template=template)

    async def attach(
        self, state: str, template: str, body: AttachBody, *, skip_reconcilers: bool = False
    ) -> list[HeldPendingSave]:
        """Attach ``template`` to ``state`` with ``body``; ``skip_reconcilers`` bypasses reconcilers.

        Returns the saves held by a failed save the validators and reconcilers read past.
        """
        return await self._app._states_service.attach(state, template, body, skip_reconcilers=skip_reconcilers)

    async def update_attachment_declarations(
        self,
        state: str,
        template: str,
        declarations: dict[str, Any],
        *,
        options: dict[str, Any] | None = None,
        skip_reconcilers: bool = False,
    ) -> list[HeldPendingSave]:
        """Update the per-template ``declarations`` of ``template``'s attachment on ``state``.

        Returns the saves held by a failed save the validators and reconcilers read past.
        """
        return await self._app._states_service.update_attachment_declarations(
            state, template, declarations, options=options, skip_reconcilers=skip_reconcilers
        )

    async def detach(self, state: str, template: str) -> None:
        """Remove ``template`` from ``state``."""
        return await self._app._states_service.detach(state, template)

    # -- backup restore --
    async def drain_pending_saves(
        self, state: str, *, held: Literal["raise", "skip", "count"], scan: str
    ) -> list[HeldPendingSave]:
        """Apply ``state``'s pending saves first; a save held by a failed one is raised on, skipped or counted.

        Skeleton-only (the backup section's export/restore and other whole-state scans), off the
        ``AppStates`` protocol.
        """
        return await self._app._states_service.drain_pending_saves(state, held=held, scan=scan)

    async def drain_target_saves(self, target_kind: str, target_name: str) -> list[str]:
        """Finish the pending saves under one conversation target; the lines naming the saves that cannot finish.

        Skeleton-only (the tool-rename referee), off the ``AppStates`` protocol.
        """
        return await self._app._states_service.drain_target_saves(target_kind, target_name)

    # -- pending saves (skeleton-only operator doors, off the ``AppStates`` protocol) --
    async def list_pending_saves(
        self, *, status: Literal["outstanding", "failed"] | None, limit: int, before: int | None
    ) -> tuple[list[OutboxRow], dict[str, int]]:
        """One page of outstanding pending saves newest first, and the per-status row counts."""
        return await self._app._states_service.list_pending_saves(status=status, limit=limit, before=before)

    async def get_pending_save(self, row_id: int) -> OutboxRow | None:
        """The outstanding pending save ``row_id``, or ``None``."""
        return await self._app._states_service.get_pending_save(row_id)

    async def retry_pending_save(self, row_id: int) -> RetryOutcome:
        """Requeue the failed save ``row_id`` and apply its records; the save as it stands after."""
        return await self._app._states_service.retry_pending_save(row_id)

    async def discard_pending_save(self, row_id: int, *, principal: str | None) -> OutboxRow | None:
        """Drop the failed save ``row_id`` for good; ``None`` when it is not failed."""
        return await self._app._states_service.discard_pending_save(row_id, principal=principal)

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

    async def apply_batch(self, writes: list[StateBatchWrite]) -> list[ApplyResult]:
        """Apply an ordered write set as ONE transaction, returning an :class:`ApplyResult` per item in order.

        A failed item rolls the whole batch back; ``op_id`` idempotency holds per item and the
        transaction's connection stays hidden behind this seam.
        """
        return await self._app._states_service.apply_batch(writes)

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

    async def enqueue_batch(self, items: Sequence[StateBatchWrite]) -> UnitCommitResult:
        """Stage ``items`` into a fresh bound unit and enqueue them as one pending save.

        The door binding's write set outside any unit. Skeleton-only, off the ``AppStates`` protocol.
        """
        return await self._app._states_service.enqueue_batch(items)

    def open_unit(self) -> AbstractAsyncContextManager[StateUnit]:
        """Open a unit of work over the states facet, bound to the caller's scope for the ``async with`` block.

        The caller stages writes (and deferred calls) against the yielded unit and reads its own
        staged writes back; ``commit`` writes them as one pending save that is applied after the
        caller returns, ``discard`` drops them, and a unit left unresolved when the block exits is
        discarded at teardown.
        """
        return self._app._states_service.open_unit()

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

    async def prune_expired(self) -> PruneResult:
        """Delete every expired record; the per-state removal counts and the held saves skipped."""
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
