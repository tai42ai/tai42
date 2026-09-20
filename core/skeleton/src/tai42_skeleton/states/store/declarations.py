"""The ``state_declarations`` table — reads, guarded upsert, and record counts/field stats."""

from __future__ import annotations

import inspect
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .connection import _pool, _settings


class _DeclarationStore:
    """The ``state_declarations`` table + its record counts and per-field/per-kind stats."""

    async def get_declaration(self, name: str) -> dict[str, Any] | None:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT name, description, schema, effective_schema, subject_kinds, default_subject_kind, "
                "retention_days, updated_at FROM state_declarations WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()

    async def list_declarations(self) -> list[dict[str, Any]]:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT name, description, schema, effective_schema, subject_kinds, default_subject_kind, "
                "retention_days, updated_at FROM state_declarations ORDER BY name"
            )
            return list(await cur.fetchall())

    async def upsert_declaration(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        subject_kinds: list[str],
        default_subject_kind: str,
        retention_days: int | None,
        effective_schema: dict[str, Any] | None = None,
    ) -> None:
        """The bare declaration upsert — no guard.

        Service-level writes go through :meth:`upsert_declaration_guarded`; this raw op backs
        tests only.

        ``effective_schema`` defaults to ``schema`` (an unattached state's effective
        schema IS its base), so a caller that never touches templates stays correct.
        """
        effective = schema if effective_schema is None else effective_schema
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "INSERT INTO state_declarations "
                "(name, description, schema, effective_schema, subject_kinds, default_subject_kind, "
                "retention_days, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description, "
                "schema = EXCLUDED.schema, effective_schema = EXCLUDED.effective_schema, "
                "subject_kinds = EXCLUDED.subject_kinds, default_subject_kind = EXCLUDED.default_subject_kind, "
                "retention_days = EXCLUDED.retention_days, updated_at = now()",
                (
                    name,
                    description,
                    Jsonb(schema),
                    Jsonb(effective),
                    Jsonb(list(subject_kinds)),
                    default_subject_kind,
                    retention_days,
                ),
            )

    async def upsert_declaration_guarded(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        subject_kinds: list[str],
        default_subject_kind: str,
        retention_days: int | None,
        *,
        effective_schema: dict[str, Any],
        decide: Any,
    ) -> None:
        """Guarded upsert, closing the narrowing check-then-act race in ONE txn.

        Locks the declaration row ``FOR UPDATE`` (blocking every ``apply_ops``'s
        ``FOR SHARE``, so no record can land mid-guard), reads the existing row and the
        per-kind record counts under that lock, then calls ``decide(existing_row |
        None, per_kind_counts)`` — awaited so the decision may resolve a by-id base schema under
        the lock — which raises to refuse (aborting the txn) — and finally performs the upsert.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT schema, subject_kinds, default_subject_kind FROM state_declarations WHERE name = %s FOR UPDATE",
                (name,),
            )
            existing = await cur.fetchone()
            per_kind: dict[str, int] = {}
            if existing is not None:
                await cur.execute(
                    "SELECT subject_kind, count(*) AS n FROM state_records WHERE state = %s GROUP BY subject_kind",
                    (name,),
                )
                per_kind = {row["subject_kind"]: int(row["n"]) for row in await cur.fetchall()}
            outcome = decide(existing, per_kind)  # raises to refuse — the txn aborts
            # ``decide`` may be sync or async (the declaration door's decision resolves a by-id
            # base schema under this lock); await it only when it returns an awaitable.
            if inspect.isawaitable(outcome):
                await outcome
            await cur.execute(
                "INSERT INTO state_declarations "
                "(name, description, schema, effective_schema, subject_kinds, default_subject_kind, "
                "retention_days, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description, "
                "schema = EXCLUDED.schema, effective_schema = EXCLUDED.effective_schema, "
                "subject_kinds = EXCLUDED.subject_kinds, default_subject_kind = EXCLUDED.default_subject_kind, "
                "retention_days = EXCLUDED.retention_days, updated_at = now()",
                (
                    name,
                    description,
                    Jsonb(schema),
                    Jsonb(effective_schema),
                    Jsonb(list(subject_kinds)),
                    default_subject_kind,
                    retention_days,
                ),
            )

    async def delete_declaration(self, name: str) -> bool:
        """Delete a state with its records, attachments, aliases and write ledger in ONE txn under the lock.

        ``False`` when no declaration exists (nothing deleted). The consumer-binding refusal
        is the service's (it consults the registered consumer listers before calling this).
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (name,))
            if await cur.fetchone() is None:
                return False
            await cur.execute("DELETE FROM state_writes WHERE state = %s", (name,))
            await cur.execute("DELETE FROM state_subject_aliases WHERE state = %s", (name,))
            await cur.execute("DELETE FROM state_records WHERE state = %s", (name,))
            await cur.execute("DELETE FROM state_attachments WHERE state = %s", (name,))
            await cur.execute("DELETE FROM state_declarations WHERE name = %s", (name,))
            return True

    async def count_records(self, state: str) -> int:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT count(*) AS n FROM state_records WHERE state = %s", (state,))
            row = await cur.fetchone()
            return 0 if row is None else int(row["n"])

    async def count_records_for_target(self, target_kind: str, target_name: str) -> int:
        """Records addressed under one conversation target ``(target_kind, target_name)`` across every state.

        The rename referee's evidence that renaming a ``tool`` target would strand its
        subject records.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT count(*) AS n FROM state_records WHERE target_kind = %s AND target_name = %s",
                (target_kind, target_name),
            )
            row = await cur.fetchone()
            return 0 if row is None else int(row["n"])

    async def field_stats(self, state: str) -> tuple[int, dict[str, int], dict[str, int]]:
        """``(record_count, per_field, per_kind)`` for the listing/stats dialog.

        A top-level key is present in ``data`` only while it holds data, so a per-key count
        IS the count of records holding that field; ``per_kind`` counts records by subject
        kind.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT count(*) AS n FROM state_records WHERE state = %s", (state,))
            count_row = await cur.fetchone()
            records = 0 if count_row is None else int(count_row["n"])
            await cur.execute(
                "SELECT key, count(*) AS n FROM state_records, jsonb_object_keys(data) AS key "
                "WHERE state = %s GROUP BY key",
                (state,),
            )
            per_field = {row["key"]: int(row["n"]) for row in await cur.fetchall()}
            await cur.execute(
                "SELECT subject_kind, count(*) AS n FROM state_records WHERE state = %s GROUP BY subject_kind",
                (state,),
            )
            per_kind = {row["subject_kind"]: int(row["n"]) for row in await cur.fetchall()}
            return records, per_field, per_kind
