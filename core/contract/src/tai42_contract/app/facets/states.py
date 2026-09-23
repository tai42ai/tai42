"""``AppStates`` — the subject-keyed state store namespace (``app.states``)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tai42_contract.states.models import (
    ApplyResult,
    AttachBody,
    AttachReconciler,
    AttachValidator,
    ConsumerLister,
    ConsumerRow,
    StateBatchWrite,
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


@runtime_checkable
class AppStates(Protocol):
    """The subject-keyed state store namespace (``app.states``).

    The door-agnostic contract every door and tool reads and writes a subject's document through.
    The store takes a full :class:`~tai42_contract.states.StateSubject` and refuses
    anything less; subject resolution happens once per door, never here. Every write
    door supplies a :class:`~tai42_contract.states.WriteOrigin` carrying only what a
    consumer knows — the platform completes it (``door``/``actor``/``turn_id``) at this
    chokepoint, so the audit ledger is never optional or forgeable. While the ``states``
    component's database is unbound every method raises
    :class:`~tai42_contract.states.StatesNotConfiguredError` (501), never an empty read.
    """

    # --- Declarations ---
    async def list_declarations(self) -> list[StateDeclaration]:
        """Every declared state."""
        ...

    async def get_declaration(self, name: str) -> StateDeclaration | None:
        """The declaration named ``name``, or ``None`` when none is declared."""
        ...

    async def put_declaration(self, decl: StateDeclaration) -> StateDeclaration:
        """Declare or re-declare a state and return the stored declaration.

        An additive re-declare (new optional fields, new subject kinds) is applied in
        place; a change that would remove or narrow a field while records exist raises
        :class:`~tai42_contract.states.NonAdditiveRedeclareError` (erase the records
        first), and removing a subject kind still present in records raises
        :class:`~tai42_contract.states.DeclarationInUseError`. Never a silent overwrite.
        """
        ...

    async def delete_declaration(self, name: str) -> None:
        """Delete a state with its records and attachments.

        Raises :class:`~tai42_contract.states.DeclarationInUseError` when a consumer still binds it.
        """
        ...

    async def stats(self, name: str) -> dict[str, Any]:
        """Counts for a state — records, subjects by kind, consumers — for the listing."""
        ...

    # --- Templates ---
    async def list_templates(self) -> list[StateTemplateDocument]:
        """Every stored platform template document."""
        ...

    async def get_template(self, name: str) -> StateTemplateDocument | None:
        """The template document named ``name``, or ``None`` when none is stored.

        The read a consumer's own sibling document validates against.
        """
        ...

    async def put_template(self, doc: StateTemplateDocument, *, replace: bool) -> StateTemplateDocument:
        """Store a template document and return it (``replace`` required to overwrite an existing name).

        Runs every registered attach validator over each live attachment before the write,
        so a consumer's data-dependent check still fires here; a raise leaves the stored
        document untouched. Overwriting an existing name without ``replace`` raises
        :class:`~tai42_contract.states.TemplateExistsError`.
        """
        ...

    async def delete_template(self, name: str) -> None:
        """Delete a template document.

        Raises :class:`~tai42_contract.states.TemplateInUseError` while it is still attached.
        """
        ...

    # --- Attachments ---
    async def list_attachments(self, state: str | None = None, *, template: str | None = None) -> list[dict[str, Any]]:
        """Attachment rows filtered by ``state``, by ``template``, or every attachment when both are ``None``.

        What the attachments listing and the Consumers tab read. Each row carries ``state``,
        ``template``, ``path``, ``parameters`` and ``declarations``.
        """
        ...

    async def attach(self, state: str, template: str, body: AttachBody) -> None:
        """Attach ``template`` on ``state`` at ``body.path``, recomposing the effective schema in one transaction.

        Stores the resolved parameters and declarations. Runs every registered attach validator before the write; a
        raise refuses the door with the validator's message. Overlapping fragments raise
        :class:`~tai42_contract.states.AttachConflictError`.
        """
        ...

    async def update_attachment_declarations(
        self, state: str, template: str, declarations: dict[str, Any], *, options: dict[str, Any] | None = None
    ) -> None:
        """Replace an attachment's declaration values, recomposing the effective schema before the write.

        Re-runs every registered attach validator and reconciler before the write.
        ``options`` is a per-operation directive bag passed to the reconcilers for THIS
        operation only, never stored or served back (``None`` is an empty bag).
        """
        ...

    async def detach(self, state: str, template: str) -> None:
        """Remove an attachment and recompose the state's effective schema."""
        ...

    # --- Records ---
    async def read(self, state: str, subject: StateSubject) -> StateRecord | None:
        """The record for ``subject`` (resolving a fold to its canonical subject), or ``None`` when none exists.

        An unknown person or a target mismatch is a refusal, never an empty document.
        """
        ...

    async def replace(
        self, state: str, subject: StateSubject, data: dict[str, Any], *, origin: WriteOrigin
    ) -> StateRecord:
        """Replace ``subject``'s whole document with ``data`` and return the new record."""
        ...

    async def merge(
        self, state: str, subject: StateSubject, patch: dict[str, Any], *, origin: WriteOrigin
    ) -> StateRecord:
        """Shallow top-level merge ``patch`` into ``subject``'s document and return the new record."""
        ...

    async def apply(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: WriteOrigin,
    ) -> ApplyResult:
        """Apply an op batch to ``subject``'s document under the effective schema.

        Refuses a whole-path write over a ``composing`` path
        (:class:`~tai42_contract.states.RegimeViolationError`) before the ledger insert,
        stamps ``_trace`` under a traced attachment, and records one ``state_writes`` row with
        the touched paths and the completed origin. A replayed ``op_id`` returns
        ``applied=False`` without re-writing; guarded ops land in ``skipped``.
        """
        ...

    async def eval_template_jq(
        self, state: str, subject: StateSubject, name: str, params: dict[str, Any]
    ) -> TemplateJqResult:
        """Evaluate an ``input``-purpose ``template_jq`` program ``name`` for ``subject`` and return its value.

        The program's jq runs over the subject's record, ``params`` supplying
        its declared parameters. ``name`` resolves across the state's attached templates
        (unqualified → the declaring template; ``<attachment>.<name>`` → that attachment's
        template). An unknown name is a :class:`~tai42_contract.states.StateNotFoundError`; an
        ambiguous unqualified name, an ``update``-purpose name, an undeclared param or an
        evaluation failure is a :class:`~tai42_contract.states.ValueValidationError`. A read;
        no write is recorded.
        """
        ...

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
        """Apply an ``update``-purpose ``template_jq`` program ``name`` to ``subject``.

        Its jq maps the record subtree (its ``.``) with ``$input`` bound to a template-relative op
        batch, rebased under the attachment path and applied through the same chokepoint as
        :meth:`apply` — so regimes, the
        composing-shape guard, trace stamping and ``op_id`` idempotency all hold. ``name``
        resolves as for :meth:`eval_template_jq`. An unknown name is a
        :class:`~tai42_contract.states.StateNotFoundError`; an ambiguous unqualified name, an
        ``input``-purpose name, an evaluation failure or a wrong-shaped return is a
        :class:`~tai42_contract.states.ValueValidationError`.
        """
        ...

    async def apply_batch(self, writes: list[StateBatchWrite]) -> list[ApplyResult]:
        """Apply an ordered write set as ONE transaction, returning an :class:`ApplyResult` per item in order.

        Each :class:`~tai42_contract.states.StateBatchWrite` is dispatched by its shape — an
        ``ops`` batch through :meth:`apply`, a ``template_jq`` program through
        :meth:`apply_template_jq` (its outcome mapped onto the item's ``ApplyResult``) — over the
        SAME store transaction, so items on one subject read each other's uncommitted writes in
        order. Any item that raises rolls the WHOLE batch back and propagates loudly: no partial
        write survives. ``op_id`` idempotency holds per item (a replayed key returns
        ``applied=False`` without re-writing while its siblings land). The transaction's connection
        stays hidden behind this seam — a door passes only the write set, never threads a
        connection. An empty list is a no-op returning ``[]``.
        """
        ...

    async def erase(self, state: str, subject: StateSubject, *, origin: WriteOrigin) -> None:
        """Erase ``subject``'s record, recording the write."""
        ...

    async def fold(
        self, state: str, subject: StateSubject, into: StateSubject, mode: str, *, origin: WriteOrigin
    ) -> dict[str, Any]:
        """Fold ``subject`` into ``into`` and return the resulting canonical document.

        ``mode`` decides how the documents combine. A self-fold, a cycle, a conflicting re-fold,
        or a merge whose result fails the schema raises
        :class:`~tai42_contract.states.SubjectFoldError`; a retried fold to the same target is a quiet no-op.
        """
        ...

    async def list_subjects(
        self, state: str, *, kind: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """A keyset page of subjects for ``state`` (optionally one ``kind``).

        ``{"subjects": [...], "next_cursor": <cursor|None>}``.
        """
        ...

    async def search(
        self, state: str, filters: dict[str, Any], *, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """A keyset page of records whose document contains ``filters``.

        ``{"matches": [...], "next_cursor": <cursor|None>}``.
        """
        ...

    async def writes(
        self, state: str, subject: StateSubject, *, limit: int | None = None, cursor: str | None = None
    ) -> WritesPage:
        """One keyset page of the audit trail for ``subject``, newest first.

        The ``items`` (each a write with its completed origin and touched paths) plus the
        ``next_cursor`` that pages the trail like ``list_subjects``/``search`` page
        theirs (the last row's id when the page is full, else ``None``).
        """
        ...

    async def prune_expired(self) -> dict[str, int]:
        """Delete records past their state's ``retention_days`` and return the per-state deletion counts."""
        ...

    # --- Ambient context (read-only; the doors deposit it, not consumers) ---
    def context(self) -> StateContext | None:
        """The ambient :class:`~tai42_contract.states.StateContext` the current door deposited.

        ``None`` outside a door (an ``api`` write completes from the request principal instead).
        """
        ...

    # --- Consumers ---
    def register_consumer_lister(self, kind: str, lister: ConsumerLister) -> None:
        """Register a lister for consumer ``kind`` (flow / hook / schedule / agent).

        A plugin calls this through the ``tai42_app`` handle when its module loads;
        ``consumers`` unions every registered lister. Registering two listers for one
        kind raises loudly.
        """
        ...

    async def consumers(self, state: str) -> list[ConsumerRow]:
        """Everything that binds ``state`` — the union of every registered consumer lister — for the Consumers tab."""
        ...

    # --- Seeds ---
    def register_template_seed(self, doc: StateTemplateDocument) -> None:
        """Declare a platform template document the platform seeds at import time.

        A plugin calls this through the ``tai42_app`` handle when its module loads. The
        startup/reload seed applier creates it when absent and leaves a template already
        present untouched. Declaring two seeds under one name raises loudly.
        """
        ...

    # --- Attach validation ---
    def register_attach_validator(self, validator: AttachValidator) -> None:
        """Register a data-dependent attach validator.

        A plugin calls this through the ``tai42_app`` handle when its module loads. The
        validator receives the template document, an attachment's declaration values, and the
        state's effective schema, and RAISES to refuse; it runs before every ``attach``,
        ``update_attachment_declarations`` and ``put_template(replace=True)`` write, so a
        consumer's checks fire at the platform's declarations doors, the States page
        included.
        """
        ...

    def register_attach_reconciler(self, reconciler: AttachReconciler) -> None:
        """Register a pre-write attach reconciler.

        A plugin calls this through the ``tai42_app`` handle when its module loads. The
        reconciler receives a :class:`~tai42_contract.states.AttachReconcileContext` — the
        template, the operation, the previous and new declarations, the attach options, and a
        record door bound to the state — inside every ``attach`` and
        ``update_attachment_declarations`` write, after the validators and before the write. It
        RAISES to refuse the attach (naming the records the new declarations orphan) or
        writes resolutions through the context's record door and returns, letting the attach
        commit with those writes.
        """
        ...
