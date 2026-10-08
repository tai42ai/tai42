"""The shared shape the service concern-mixins are typed against.

``StatesService`` is composed from concern mixins that each live in their own module and
call one another's methods and the shared instance state through ``self``. This base
declares that state and the cross-mixin method surface (each concrete method overrides its
declaration here), so every mixin resolves its ``self`` access against one place.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from psycopg import AsyncConnection
    from tai42_contract.states.models import (
        ApplyResult,
        AttachReconciler,
        CompletedOrigin,
        ConsumerRow,
        StateBatchWrite,
        StateContext,
        StateDeclaration,
        StateRecord,
        StateSubject,
        StateTemplateDocument,
        TemplateJqApplyResult,
        WriteOrigin,
    )
    from tai42_contract.template import TemplatedText

    from tai42_skeleton.states.seeds import StateTemplateSeedRegistry
    from tai42_skeleton.states.store import PostgresStatesStore
    from tai42_skeleton.states.templates import StateTemplate

    from .catalog import CatalogSnapshot, StateEntry, TemplateEntry
    from .registries import (
        StatesAttachReconcilerRegistry,
        StatesAttachValidatorRegistry,
        StatesConsumerListerRegistry,
    )
    from .rendered import RenderedEntry, RenderedTemplates
    from .unit import StagedItemRecord, _StagedReplace, _StateUnit


class _StatesServiceBase:
    """Instance state + the cross-mixin method surface of :class:`StatesService`."""

    _store: PostgresStatesStore
    _attach_validators: StatesAttachValidatorRegistry
    _attach_reconcilers: StatesAttachReconcilerRegistry
    _consumer_listers: StatesConsumerListerRegistry
    _seeds: StateTemplateSeedRegistry
    _catalog: CatalogSnapshot
    _rendered_cache: RenderedTemplates

    @staticmethod
    def _ensure_available() -> None: ...

    def context(self) -> StateContext | None: ...

    def _complete_origin(self, origin: WriteOrigin) -> CompletedOrigin: ...

    async def _admit_subject(self, state: str, subject: StateSubject) -> tuple[int, list[str], str]: ...

    async def _validate_subject_admitted(
        self, subject_kinds: list[str], state_name: str, subject: StateSubject
    ) -> None: ...

    async def _require_declaration(self, state: str) -> dict[str, Any]: ...

    async def _require_declaration_decl(self, state: str) -> StateDeclaration: ...

    async def _get_template_or_raise(self, name: str) -> StateTemplate: ...

    async def _resolve_template_body(self, name: str, body: dict[str, Any]) -> tuple[dict[str, Any], bool]: ...

    async def _template_entry(self, name: str) -> TemplateEntry | None: ...

    async def _template_at(self, name: str, version: int, body: dict[str, Any]) -> StateTemplate: ...

    async def _rendered(self, template: StateTemplate, version: int) -> RenderedEntry: ...

    async def _validated_template_write(self, doc: StateTemplateDocument) -> tuple[StateTemplate, dict[str, Any]]: ...

    async def _load_state_attachments(
        self, state: str, *, override: dict[str, StateTemplate] | None = None
    ) -> list[tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any]]]: ...

    async def _entry_attachments(
        self, entry: StateEntry, *, override: dict[str, StateTemplate] | None = None
    ) -> list[tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any]]]: ...

    async def _compose_effective(self, state: str, base_schema: TemplatedText | dict[str, Any]) -> dict[str, Any]: ...

    @staticmethod
    def _effective_parameters(template: StateTemplate, parameters: dict[str, Any]) -> dict[str, Any]: ...

    async def _validate_attach_values(
        self, template: StateTemplate, parameters: dict[str, Any], declarations: dict[str, Any]
    ) -> None: ...

    async def _run_attach_validators(
        self, state: str, template_doc: StateTemplateDocument, declarations: dict[str, Any], effective: dict[str, Any]
    ) -> None: ...

    async def _run_attach_reconcilers(
        self,
        reconcilers: list[AttachReconciler],
        state: str,
        template_doc: StateTemplateDocument,
        operation: Literal["attach", "update_declarations"],
        *,
        previous_declarations: dict[str, Any] | None,
        new_declarations: dict[str, Any],
        options: dict[str, Any],
        conn: AsyncConnection[Any],
    ) -> None: ...

    async def read(
        self, state: str, subject: StateSubject, *, conn: AsyncConnection[Any] | None = None
    ) -> StateRecord | None: ...

    async def replace(
        self,
        state: str,
        subject: StateSubject,
        data: dict[str, Any],
        *,
        origin: WriteOrigin,
        conn: AsyncConnection[Any] | None = None,
    ) -> StateRecord: ...

    async def apply(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: WriteOrigin,
        conn: AsyncConnection[Any] | None = None,
    ) -> ApplyResult: ...

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
    ) -> TemplateJqApplyResult: ...

    async def apply_batch(self, writes: list[StateBatchWrite]) -> list[ApplyResult]: ...

    async def _commit_writes(
        self,
        writes: Sequence[StateBatchWrite | _StagedReplace],
        *,
        staged: Sequence[StagedItemRecord] | None = None,
        conn: AsyncConnection[Any] | None = None,
    ) -> list[ApplyResult]: ...

    async def _replace_completed(
        self,
        state: str,
        subject: StateSubject,
        data: dict[str, Any],
        *,
        origin: CompletedOrigin,
        conn: AsyncConnection[Any] | None = None,
        validate: bool = True,
    ) -> StateRecord: ...

    async def _apply_completed(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: CompletedOrigin,
        conn: AsyncConnection[Any] | None = None,
        validate: bool = True,
    ) -> ApplyResult: ...

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
    ) -> TemplateJqApplyResult: ...

    async def _resolve_template_jq_ops(
        self,
        state: str,
        subject: StateSubject,
        name: str,
        input_: Any,
        *,
        conn: AsyncConnection[Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def open_unit(self) -> AbstractAsyncContextManager[_StateUnit]: ...

    async def _projected_record_view(
        self, state: str, subject: StateSubject, *, conn: AsyncConnection[Any] | None = None
    ) -> dict[str, Any] | None: ...

    async def list_subjects(
        self,
        state: str,
        *,
        kind: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        conn: AsyncConnection[Any] | None = None,
    ) -> dict[str, Any]: ...

    async def consumers(self, state: str) -> list[ConsumerRow]: ...
