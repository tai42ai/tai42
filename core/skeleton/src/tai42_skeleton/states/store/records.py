"""Alias-aware ``state_records`` reads — subject resolution, single-record and view reads, and export reads."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from tai42_contract.states.models import StateSubject
from tai42_kit.clients.impl.postgres import read_connection

from tai42_skeleton.states.outbox.keys import record_key
from tai42_skeleton.states.outbox.models import OutboxPending

from .base import _StoreBase
from .connection import _pool, _settings


class _RecordReadStore(_StoreBase):
    """Alias-aware reads over ``state_records`` and ``state_subject_aliases``."""

    async def _resolve_subject(
        self, cur: Any, state: str, subject: StateSubject, *, check_outbox: bool
    ) -> tuple[str, str]:
        """The canonical ``(kind, key)`` for ``subject`` — one indexed lookup on the CALLER's cursor.

        Joins the caller's transaction (and its declaration-row lock, which serializes against every fold).
        No alias row ⇒ the subject IS its own canonical. Target scope never changes across a fold.
        ``check_outbox`` (a caller on its own pooled connection) folds the pending-save check into the
        same statement and raises :class:`OutboxPending` when the subject's record has an unapplied
        pending save; a transaction-threaded caller passes ``False``.
        """
        if not check_outbox:
            await cur.execute(
                "SELECT canonical_kind, canonical_key FROM state_subject_aliases "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND alias_kind = %s AND alias_key = %s",
                (state, subject.target_kind, subject.target_name, subject.kind, subject.key),
            )
            row = await cur.fetchone()
            return (subject.kind, subject.key) if row is None else (row["canonical_kind"], row["canonical_key"])
        keys = [record_key(state, subject)]
        await cur.execute(
            "SELECT a.canonical_kind, a.canonical_key, EXISTS (SELECT 1 FROM state_outbox o "
            "WHERE o.record_keys && %(keys)s::text[] "
            "AND (o.status = 'pending' OR (o.status = 'failed' AND o.records_applied_at IS NULL))) AS outbox_pending "
            "FROM (SELECT 1) AS one LEFT JOIN state_subject_aliases a "
            "ON a.state = %(state)s AND a.target_kind = %(tk)s AND a.target_name = %(tn)s "
            "AND a.alias_kind = %(kind)s AND a.alias_key = %(key)s",
            {
                "keys": keys,
                "state": state,
                "tk": subject.target_kind,
                "tn": subject.target_name,
                "kind": subject.kind,
                "key": subject.key,
            },
        )
        row = await cur.fetchone()
        if row is None:
            raise AssertionError("the subject-resolve statement returns one row")
        if row["outbox_pending"]:
            raise OutboxPending(keys)
        if row["canonical_kind"] is None:
            return subject.kind, subject.key
        return row["canonical_kind"], row["canonical_key"]

    async def read_record(self, state: str, subject: StateSubject) -> tuple[dict[str, Any] | None, float | None]:
        """``(data, seq)`` for ``subject``, resolved through the alias table; ``(None, None)`` when absent.

        A folded key lands on the surviving record.
        """
        async with (
            _pool(_settings()) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            kind, key = await self._resolve_subject(cur, state, subject, check_outbox=True)
            await cur.execute(
                "SELECT data, extract(epoch FROM updated_at)::float8 AS seq FROM state_records "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s AND subject_key = %s",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            row = await cur.fetchone()
            return (None, None) if row is None else (row["data"], row["seq"])

    async def read_record_view(
        self,
        state: str,
        subject: StateSubject,
        *,
        conn: AsyncConnection[Any] | None = None,
        check_outbox: bool = True,
    ) -> dict[str, Any] | None:
        """The read door's view: ``{data, seq, canonical_subject, folded_from}``, or ``None`` when absent.

        ``folded_from`` is every alias pointing at the canonical. With ``conn`` the read joins the caller's
        transaction (an attach reconciler reading its own in-flight merges). A read on its own pooled
        connection raises :class:`OutboxPending` when the subject has an unapplied pending save, unless
        ``check_outbox`` is ``False`` (a read-back of a write that just landed).
        """
        async with self._read_cursor(conn) as cur:
            kind, key = await self._resolve_subject(cur, state, subject, check_outbox=check_outbox and conn is None)
            await cur.execute(
                "SELECT data, extract(epoch FROM updated_at)::float8 AS seq FROM state_records "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s AND subject_key = %s",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await cur.execute(
                "SELECT alias_kind, alias_key FROM state_subject_aliases "
                "WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND canonical_kind = %s AND canonical_key = %s ORDER BY alias_kind, alias_key",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            folded = [
                StateSubject(
                    target_kind=subject.target_kind,
                    target_name=subject.target_name,
                    kind=r["alias_kind"],
                    key=r["alias_key"],
                )
                for r in await cur.fetchall()
            ]
            canonical = StateSubject(
                target_kind=subject.target_kind, target_name=subject.target_name, kind=kind, key=key
            )
            return {
                "data": row["data"],
                "seq": row["seq"],
                "canonical_subject": canonical,
                "folded_from": folded,
            }

    async def export_records(self, state: str) -> list[dict[str, Any]]:
        """Every record of a state as export rows ``{target_kind, target_name, subject_kind, subject_key, data}``.

        The backup exporter's read.
        """
        async with (
            _pool(_settings()) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT target_kind, target_name, subject_kind, subject_key, data FROM state_records "
                "WHERE state = %s ORDER BY target_kind, target_name, subject_kind, subject_key",
                (state,),
            )
            return list(await cur.fetchall())

    async def list_aliases(self, state: str) -> list[dict[str, Any]]:
        """Every subject-alias row of a state — the backup exporter's read."""
        async with (
            _pool(_settings()) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT target_kind, target_name, alias_kind, alias_key, canonical_kind, canonical_key, mode "
                "FROM state_subject_aliases WHERE state = %s "
                "ORDER BY target_kind, target_name, alias_kind, alias_key",
                (state,),
            )
            return list(await cur.fetchall())
