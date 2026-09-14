"""Consumer registration and the seams that run registered validators/reconcilers.

A consumer registers a lister, an attach validator, an attach reconciler, or a template
seed; the attach doors run every registered validator (before any write) then every
reconciler (on the attach transaction, before the write), and the seed applier creates each
shipped template seed absent from the store while the feature is on.
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg import AsyncConnection
from tai42_contract.states.errors import TemplateValidationError
from tai42_contract.states.models import (
    AttachReconcileContext,
    AttachReconciler,
    AttachValidator,
    ConsumerLister,
    ConsumerRow,
    StateTemplateDocument,
)

from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.reconcile_support import _AttachReconcileRecords


class _RegistrationMixin(_StatesServiceBase):
    def register_consumer_lister(self, kind: str, lister: ConsumerLister) -> None:
        self._consumer_listers.register(kind, lister)

    async def consumers(self, state: str) -> list[ConsumerRow]:
        """Everything that binds ``state`` — the union of every registered consumer
        lister."""
        self._ensure_available()
        rows: list[ConsumerRow] = []
        for lister in self._consumer_listers.all().values():
            rows.extend(await lister(state))
        return rows

    def register_attach_validator(self, validator: AttachValidator) -> None:
        self._attach_validators.register(validator)

    async def _run_attach_validators(
        self, template_doc: StateTemplateDocument, declarations: dict[str, Any], effective: dict[str, Any]
    ) -> None:
        """Run every registered attach validator with the template document, the attach's
        declaration values, and the state's effective schema — BEFORE any write. A validator
        raises loudly (a :class:`TemplateValidationError`) to refuse the door."""
        for validator in self._attach_validators.all():
            await validator(template_doc, declarations, effective)

    def register_attach_reconciler(self, reconciler: AttachReconciler) -> None:
        self._attach_reconcilers.register(reconciler)

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
    ) -> None:
        """Run each attach reconciler AFTER the validators and BEFORE the write, each with a
        :class:`AttachReconcileContext` whose record door writes on the caller's transaction
        ``conn`` — so a reconciler's writes commit with the attach or roll back together with
        a refusal. A reconciler raises (a :class:`TemplateValidationError`, named with the
        template and state) to refuse the attach, or writes resolutions through the record door
        and returns so the attach commits with them. Any other exception propagates with the
        template and state named — never swallowed."""
        context = AttachReconcileContext(
            state=state,
            template=template_doc,
            operation=operation,
            previous_declarations=previous_declarations,
            new_declarations=new_declarations,
            options=options,
            records=_AttachReconcileRecords(self, state, conn),
        )
        for reconciler in reconcilers:
            try:
                await reconciler(context)
            except TemplateValidationError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"attach reconciler for template {template_doc.name!r} on state {state!r} failed: {exc}"
                ) from exc

    def register_template_seed(self, doc: StateTemplateDocument) -> None:
        self._seeds.register(doc)

    async def apply_template_seeds(self) -> None:
        """Create each shipped template seed that is absent from the store (a no-op while the
        feature is off)."""
        from tai42_skeleton.states import service as _pkg

        if not _pkg.states_store_configured():
            return
        from tai42_skeleton.states.seeds import apply_template_seeds

        await apply_template_seeds(self._store, seeds=self._seeds.seeds())
