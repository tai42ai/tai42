"""The state-declaration lifecycle: list, get, create/re-declare, delete, and stats.

A re-declare accepts only ADDITIVE schema changes while records exist and refuses removing a
subject kind still present in records; ``retention_days`` is metadata, never gated.
"""

from __future__ import annotations

import logging
from time import monotonic
from typing import Any

from jsonschema import Draft202012Validator
from tai42_contract.states.errors import (
    DeclarationInUseError,
    NonAdditiveRedeclareError,
    StateNotFoundError,
    ValueValidationError,
)
from tai42_contract.states.models import StateDeclaration, StateSubject
from tai42_contract.states.pending import HeldPendingSave, StateDeclarationSaved
from tai42_contract.template import TemplatedText

from tai42_skeleton.states.db import states_settings
from tai42_skeleton.states.outbox.drain import drain_state, held_subjects
from tai42_skeleton.states.outbox.models import HeldRecord
from tai42_skeleton.states.schema import _is_narrowing, _validate_document, _validate_schema
from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.catalog import StateEntry, served_row
from tai42_skeleton.states.service.rows import _resolve_state_schema, _row_to_declaration

logger = logging.getLogger(__name__)


class _DeclarationMixin(_StatesServiceBase):
    async def list_declarations(self) -> list[StateDeclaration]:
        self._ensure_available()
        out: list[StateDeclaration] = []
        for row in await self._store.list_declarations():
            entry = await self._catalog.catalog_entry_at(row["name"], int(row["version"]))
            if entry is None:
                continue  # deleted between the list and this read
            out.append(_served_declaration(entry))
        return out

    async def get_declaration(self, name: str) -> StateDeclaration | None:
        """The declaration from the catalog snapshot after one ``SELECT version`` probe; ``None`` when undeclared."""
        self._ensure_available()
        entry = await self._catalog.catalog_entry(name)
        return None if entry is None else _served_declaration(entry)

    async def put_declaration(self, decl: StateDeclaration) -> StateDeclarationSaved:
        """Create or plain re-declare a state.

        With records present, a re-declare accepts only ADDITIVE schema changes; a removal
        or change of an existing property is refused while records exist
        (:class:`NonAdditiveRedeclareError`), and removing a subject kind still present in
        records raises :class:`DeclarationInUseError`. ``retention_days`` is metadata, not
        schema, so changing it alone is never gated. The state's pending saves are applied
        first; a save held by a failed save counts as records present: a re-declare that would
        refuse a held save's document, or remove a subject kind it writes, raises
        :class:`DeclarationInUseError`, and an accepted one names the held saves.
        """
        self._ensure_available()
        if decl.effective_schema is not None:
            raise ValueError("effective_schema is computed by the platform")
        if decl.regimes is not None:
            raise ValueError("regimes are computed by the platform")
        if decl.updated_at is not None:
            raise ValueError("updated_at is set by the platform")
        # The base ``schema`` is the ``TemplatedText | dict`` union: resolve it ONCE here — the
        # save-and-use door — to its schema dict for validation, effective composition and the
        # narrowing check. An unfetchable by-id schema (or one that does not render to a JSON
        # object) fails the save loudly, naming the field, never a state declared on an empty
        # schema. The UNION is stored as-is (baseline), so a read serves the by-id reference back.
        resolved_schema = await _resolve_state_schema(f"state {decl.name!r} schema", decl.schema_)
        _validate_schema(resolved_schema)
        effective_schema = await self._compose_effective(decl.name, resolved_schema)
        stored_schema = decl.schema_.model_dump() if isinstance(decl.schema_, TemplatedText) else decl.schema_
        deadline = monotonic() + states_settings().outbox_drain_timeout_seconds
        held_saves = await drain_state(self, decl.name, deadline, held="count", scan="re-declare")

        async def decide(existing: dict[str, Any] | None, per_kind: dict[str, int], held: list[HeldRecord]) -> None:
            if existing is None:
                return
            total = sum(per_kind.values())
            if total > 0:
                # Resolve the CURRENT stored base schema under the guard's lock so a by-id
                # narrowing comparison sees the live schema, never a pre-read stale one.
                existing_schema = await _resolve_state_schema(f"state {decl.name!r} stored schema", existing["schema"])
                if _is_narrowing(existing_schema, resolved_schema):
                    raise NonAdditiveRedeclareError(
                        f"state {decl.name!r} has records: removing or changing a field is refused while records "
                        f"exist — erase them first"
                    )
            removed = set(existing["subject_kinds"]) - set(decl.subject_kinds)
            in_use = sorted(k for k in removed if per_kind.get(k, 0) > 0)
            if in_use:
                raise DeclarationInUseError(
                    f"state {decl.name!r} still has records under subject kind(s) {in_use}; erase them before "
                    f"removing the kind(s)"
                )
            _refuse_for_held(decl, removed, held, held_saves, effective_schema)

        saved_held = await self._store.upsert_declaration_guarded(
            decl.name,
            decl.description,
            stored_schema,
            decl.subject_kinds,
            decl.default_subject_kind,
            decl.retention_days,
            effective_schema=effective_schema,
            decide=decide,
            held_saves=held_saves,
        )
        if saved_held:
            subjects = held_subjects(saved_held)
            logger.warning(
                "states: re-declare of state %r accepted with %d subject(s) held by failed pending save(s) %s: %s",
                decl.name,
                len(subjects),
                sorted({h.held_by for h in saved_held}),
                subjects,
            )
        return StateDeclarationSaved(declaration=decl, held=saved_held)

    async def delete_declaration(self, name: str) -> None:
        """Delete a state with its records, attachments and aliases.

        Refuses while a registered consumer still binds it (:class:`DeclarationInUseError`).
        """
        self._ensure_available()
        if await self._store.declaration_version(name) is None:
            raise StateNotFoundError(f"no state declared as {name!r}")
        # Deleting the state under a held save would leave a save whose declaration is gone.
        deadline = monotonic() + states_settings().outbox_drain_timeout_seconds
        await drain_state(self, name, deadline, held="raise", scan="delete_declaration")
        consumers = await self.consumers(name)
        binders = [c for c in consumers if c.unavailable is None]
        if binders:
            names = ", ".join(sorted(f"{c.kind}:{c.name}" for c in binders if c.name))
            raise DeclarationInUseError(
                f"state {name!r} is still bound by {names or 'a consumer'} — remove the binding(s) first"
            )
        await self._store.delete_declaration(name)

    async def stats(self, name: str) -> dict[str, Any]:
        """``{records, per_field, per_kind, consumers}`` for the listing."""
        self._ensure_available()
        decl = await self._require_declaration(name)
        deadline = monotonic() + states_settings().outbox_drain_timeout_seconds
        held = await drain_state(self, name, deadline, held="skip", scan="stats")
        records, per_field, per_kind = await self._store.field_stats(name)
        props = (await _resolve_state_schema(f"state {name!r} schema", decl["schema"])).get("properties", {})
        consumers = await self.consumers(name)
        return {
            "records": records,
            "per_field": {f: per_field.get(f, 0) for f in props},
            "per_kind": per_kind,
            "consumers": len([c for c in consumers if c.unavailable is None]),
            "held": [h.model_dump(mode="json") for h in held],
        }


def _refuse_for_held(
    decl: StateDeclaration,
    removed: set[str],
    held: list[HeldRecord],
    held_saves: list[HeldPendingSave],
    effective_schema: dict[str, Any],
) -> None:
    """Refuse a re-declare under which a held save's retry would fail.

    A subject kind a held save writes is removed, or the new effective schema refuses its document.
    """
    if not held:
        return
    by_id = {h.save_id: h for h in held_saves}

    def named(save_ids: list[str]) -> list[dict[str, Any]]:
        return [by_id[i].model_dump(mode="json") for i in save_ids if i in by_id]

    kinds = sorted({r.subject.kind for r in held if r.subject.kind in removed})
    if kinds:
        hit = [r for r in held if r.subject.kind in removed]
        ids = sorted({r.save_id for r in hit})
        raise DeclarationInUseError(
            f"state {decl.name!r} has held pending save(s) {ids} writing subject kind(s) {kinds} "
            f"(subjects {[_subject_text(r.subject) for r in hit]}); keep the kind(s), or discard the save(s)",
            extra={"held": named(ids)},
        )
    validator = Draft202012Validator(effective_schema)
    for record in held:
        try:
            _validate_document(validator, record.document)
        except ValueValidationError as exc:
            ids = sorted({r.save_id for r in held})
            raise DeclarationInUseError(
                f"state {decl.name!r} has held pending save(s) {ids} whose document the new declaration refuses "
                f"(save {record.save_id}, subject {_subject_text(record.subject)}: {exc}); widen the declaration "
                f"so the held save(s) validate, or discard them",
                extra={"held": named(ids)},
            ) from exc


def _subject_text(subject: StateSubject) -> str:
    return f"{subject.kind}/{subject.key}"


def _served_declaration(entry: StateEntry) -> StateDeclaration:
    """The served declaration of a catalog entry, built from deep copies of its JSON fields."""
    return _row_to_declaration(served_row(entry)).model_copy(update={"regimes": list(entry.served_regimes)})
