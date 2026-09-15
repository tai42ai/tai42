"""The state-declaration lifecycle: list, get, create/re-declare, delete, and stats.

A re-declare accepts only ADDITIVE schema changes while records exist and refuses removing a
subject kind still present in records; ``retention_days`` is metadata, never gated.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.states.errors import (
    DeclarationInUseError,
    NonAdditiveRedeclareError,
    StateNotFoundError,
)
from tai42_contract.states.models import StateDeclaration
from tai42_contract.template import TemplatedText

from tai42_skeleton.states.schema import _is_narrowing, _validate_schema
from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.rows import _resolve_state_schema, _row_to_declaration


class _DeclarationMixin(_StatesServiceBase):
    async def list_declarations(self) -> list[StateDeclaration]:
        self._ensure_available()
        out: list[StateDeclaration] = []
        for row in await self._store.list_declarations():
            decl = _row_to_declaration(row)
            regimes = self._compose_regimes(await self._load_state_attachments(decl.name))
            out.append(decl.model_copy(update={"regimes": regimes}))
        return out

    async def get_declaration(self, name: str) -> StateDeclaration | None:
        self._ensure_available()
        row = await self._store.get_declaration(name)
        if row is None:
            return None
        decl = _row_to_declaration(row)
        regimes = self._compose_regimes(await self._load_state_attachments(decl.name))
        return decl.model_copy(update={"regimes": regimes})

    async def put_declaration(self, decl: StateDeclaration) -> StateDeclaration:
        """Create or plain re-declare a state.

        With records present, a re-declare accepts only ADDITIVE schema changes; a removal
        or change of an existing property is refused while records exist
        (:class:`NonAdditiveRedeclareError`), and removing a subject kind still present in
        records raises :class:`DeclarationInUseError`. ``retention_days`` is metadata, not
        schema, so changing it alone is never gated.
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

        async def decide(existing: dict[str, Any] | None, per_kind: dict[str, int]) -> None:
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

        await self._store.upsert_declaration_guarded(
            decl.name,
            decl.description,
            stored_schema,
            decl.subject_kinds,
            decl.default_subject_kind,
            decl.retention_days,
            effective_schema=effective_schema,
            decide=decide,
        )
        return decl

    async def delete_declaration(self, name: str) -> None:
        """Delete a state with its records, attachments and aliases.

        Refuses while a registered consumer still binds it (:class:`DeclarationInUseError`).
        """
        self._ensure_available()
        if await self._store.get_declaration(name) is None:
            raise StateNotFoundError(f"no state declared as {name!r}")
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
        decl = await self._store.get_declaration(name)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {name!r}")
        records, per_field, per_kind = await self._store.field_stats(name)
        props = (await _resolve_state_schema(f"state {name!r} schema", decl["schema"])).get("properties", {})
        consumers = await self.consumers(name)
        return {
            "records": records,
            "per_field": {f: per_field.get(f, 0) for f in props},
            "per_kind": per_kind,
            "consumers": len([c for c in consumers if c.unavailable is None]),
        }
