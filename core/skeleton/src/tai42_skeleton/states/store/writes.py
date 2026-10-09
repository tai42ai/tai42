"""The record write path — replace, path-op apply, RTBF erase and subject fold.

Each records the ``state_writes`` provenance row and the ``state_applied_ops``
idempotency ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tai42_contract.states.errors import StateNotFoundError, SubjectFoldError
from tai42_contract.states.models import CompletedOrigin, StateSubject

from tai42_skeleton.states.outbox.keys import record_key
from tai42_skeleton.states.paths import apply_ops as apply_path_ops
from tai42_skeleton.states.paths import partition_guarded
from tai42_skeleton.states.schema import _validate_document

from .base import ApplyEntry, ApplyEntrySource, _StoreBase
from .connection import _pool, _settings
from .cursors import _subject_cols
from .trace import _refuse_composing_shape, stamp_trace, trace_stamp


@dataclass(frozen=True, slots=True)
class ProjectedWrite:
    """One staged write's commit bookkeeping on a compare-and-set subject write.

    ``ledger`` inserts ``op_id`` into the idempotency ledger; ``row`` records one ``state_writes``
    row with ``paths`` and the completed ``origin``.
    """

    op_id: str | None
    paths: list[list[Any]]
    origin: CompletedOrigin
    ledger: bool
    row: bool


class _ProjectionMovedError(Exception):
    """The compare-and-set found a moved base, a moved declaration version, or a ledger conflict."""


class _RecordWriteStore(_StoreBase):
    """The record write path plus the ``state_writes`` provenance row and ``state_applied_ops`` idempotency ledger."""

    @staticmethod
    async def _insert_write(
        cur: Any,
        state: str,
        tk: str,
        tn: str,
        kind: str,
        key: str,
        seq: float | None,
        origin: CompletedOrigin,
        paths: list[list[Any]],
        op_id: str | None,
    ) -> None:
        """Record one ``state_writes`` row in the caller's transaction.

        The row carries the completed origin plus the touched paths.
        """
        await cur.execute(
            "INSERT INTO state_writes "
            "(state, target_kind, target_name, subject_kind, subject_key, seq, at, door, actor, consumer, meta, "
            "run_id, turn_id, paths, op_id) VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                state,
                tk,
                tn,
                kind,
                key,
                seq,
                origin.door,
                origin.actor,
                origin.consumer,
                Jsonb(origin.meta) if origin.meta is not None else None,
                origin.run_id,
                origin.turn_id,
                Jsonb(paths),
                op_id,
            ),
        )

    async def replace(
        self,
        state: str,
        subject: StateSubject,
        data: dict[str, Any],
        *,
        origin: CompletedOrigin,
        catalog: ApplyEntrySource,
        validate_subject_in_txn: Any,
        validate: bool = True,
        conn: AsyncConnection[Any] | None = None,
        check_outbox: bool = True,
    ) -> tuple[dict[str, Any], float]:
        """Replace ``subject``'s whole document with ``data`` and record the write.

        ONE txn under the declaration ``FOR SHARE`` lock, which reads the ``version`` and the
        declared ``subject_kinds``; ``validate_subject_in_txn`` refuses the subject under that lock,
        so the write is admitted by the declaration it lands under. ``data`` is validated whole with
        the declaration version's validator (``validate=False`` leaves the check to a caller that
        validates the batch it belongs to); the write records paths ``[[]]`` (the whole document).
        The subject resolves through the alias table first. With ``conn`` the write joins the
        caller's transaction (the unit of work's commit replaying a staged replace alongside the
        batch's other writes). Without ``conn`` the write raises :class:`OutboxPending`, before
        writing, when the subject has an unapplied pending save (unless ``check_outbox`` is off).
        """
        async with self._write_cursor(conn) as cur:
            version, subject_kinds = await self._lock_declaration(cur, state, "SHARE")
            await validate_subject_in_txn(subject_kinds)
            entry = await catalog.write_entry(cur, state, version)
            kind, key = await self._resolve_subject(cur, state, subject, check_outbox=check_outbox and conn is None)
            if validate:
                _validate_document(entry.validator, data)
            await cur.execute(
                "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data, "
                "updated_at) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp()) "
                "ON CONFLICT (state, target_kind, target_name, subject_kind, subject_key) DO UPDATE SET "
                "data = EXCLUDED.data, updated_at = clock_timestamp() "
                "RETURNING extract(epoch FROM updated_at)::float8 AS seq",
                (state, subject.target_kind, subject.target_name, kind, key, Jsonb(data)),
            )
            seq_row = await cur.fetchone()
            seq = None if seq_row is None else seq_row["seq"]
            await self._insert_write(
                cur, state, subject.target_kind, subject.target_name, kind, key, seq, origin, [[]], None
            )
            return data, float(seq or 0.0)

    @staticmethod
    async def _lock_declaration(cur: Any, state: str, mode: str) -> tuple[int, list[str]]:
        """Lock the declaration row (``SHARE`` or ``UPDATE``) and read ``(version, subject_kinds)``.

        An undeclared state raises loudly.
        """
        if mode == "SHARE":
            await cur.execute(
                "SELECT version, subject_kinds FROM state_declarations WHERE name = %s FOR SHARE", (state,)
            )
        else:
            await cur.execute(
                "SELECT version, subject_kinds FROM state_declarations WHERE name = %s FOR UPDATE", (state,)
            )
        decl = await cur.fetchone()
        if decl is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        return int(decl["version"]), list(decl["subject_kinds"])

    async def read_apply_context(
        self, state: str, *, catalog: ApplyEntrySource, conn: AsyncConnection[Any] | None = None
    ) -> tuple[int, list[str], ApplyEntry]:
        """The inputs :meth:`apply_ops` validates and projects with, read WITHOUT holding a lock across a scope.

        Returns ``(version, subject_kinds, entry)`` — the declaration version, its declared subject
        kinds, and the entry (validator, composed regime paths, traced attach prefixes) built at that
        version — read under a ``FOR SHARE`` lock held only for this read, so an in-scope unit of
        work projects a staged write against the identical inputs a commit at the same version
        applies it under. An undeclared state raises loudly.
        """
        async with self._write_cursor(conn) as cur:
            version, subject_kinds = await self._lock_declaration(cur, state, "SHARE")
            entry = await catalog.write_entry(cur, state, version)
            return version, subject_kinds, entry

    async def op_applied(self, op_id: str, *, conn: AsyncConnection[Any] | None = None) -> bool:
        """Whether ``op_id`` is already in the idempotency ledger — the read a staged replay checks.

        A staged write carrying an ``op_id`` a prior commit already recorded projects as
        ``applied=False`` (no re-write), matching what :meth:`apply_ops` answers at commit.
        """
        async with self._read_cursor(conn) as cur:
            await cur.execute("SELECT 1 AS present FROM state_applied_ops WHERE op_id = %s", (op_id,))
            return await cur.fetchone() is not None

    async def apply_ops(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: CompletedOrigin,
        catalog: ApplyEntrySource,
        validate_subject_in_txn: Any,
        validate: bool = True,
        conn: AsyncConnection[Any] | None = None,
        check_outbox: bool = True,
    ) -> tuple[bool, dict[str, Any] | None, float | None, list[dict[str, Any]]]:
        """Apply a batch of path-addressed ops to one record, in ONE txn.

        Returns ``(applied, merged_document, seq, guarded_skipped)`` —
        ``(False, None, None, [])`` on an op-id replay.

        Order (pinned): the declaration row ``FOR SHARE`` — the schema-change serialization pin,
        reading the ``version`` and the declared ``subject_kinds``; ``validate_subject_in_txn``
        refuses the subject under that lock; the version's entry (validator, composed regime +
        traced paths) from ``catalog`` (a miss reads the effective schema and the attachments on
        this transaction); refuse a ``composing`` shape violation BEFORE the op-ledger insert; the
        op-ledger ``INSERT ... ON CONFLICT DO NOTHING`` when ``op_id`` is set (replay ⇒ return
        without touching the record); the ATOMIC UPSERT-LOCK on the record row; the
        COMPARE-AND-SET GUARD filter; the ``_trace`` stamp under a traced attach; the SHARED pure
        ops apply; the whole-document validation (skipped with ``validate=False``, for a caller
        that validates the batch the write belongs to); the UPDATE + ``state_writes`` row as ONE
        writable CTE. With ``conn`` the write joins the caller's transaction (a reconciler's
        record write commits or rolls back with the attach); without it the subject resolve raises
        :class:`OutboxPending`, before any write, when the subject has an unapplied pending save
        (unless ``check_outbox`` is off).
        """
        async with self._write_cursor(conn) as cur:
            version, subject_kinds = await self._lock_declaration(cur, state, "SHARE")

            # Refuse the subject inside this transaction, under the declaration lock: the one
            # locked read of the declaration serves the subject admission check AND the entry
            # the validation uses; the door does not separately pre-read the declaration.
            await validate_subject_in_txn(subject_kinds)

            entry = await catalog.write_entry(cur, state, version)
            regime_paths, traced_paths = entry.regime_paths, entry.traced_paths

            # (i) refuse a composing shape violation BEFORE the ledger insert.
            _refuse_composing_shape(ops, regime_paths)

            kind, key = await self._resolve_subject(cur, state, subject, check_outbox=check_outbox and conn is None)

            if op_id is not None:
                await cur.execute(
                    "INSERT INTO state_applied_ops (op_id, applied_at) VALUES (%s, now()) ON CONFLICT DO NOTHING",
                    (op_id,),
                )
                if cur.rowcount == 0:
                    return (False, None, None, [])

            await cur.execute(
                "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data, "
                "updated_at) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, clock_timestamp()) "
                "ON CONFLICT (state, target_kind, target_name, subject_kind, subject_key) DO UPDATE SET "
                "state = EXCLUDED.state "
                "RETURNING data, extract(epoch FROM updated_at)::float8 AS seq, (xmax = 0) AS inserted",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            record = await cur.fetchone()
            if record is None:
                raise AssertionError
            current = record["data"]

            applied_ops, guarded_skipped = partition_guarded(current, ops)

            if not applied_ops:
                if record["inserted"]:
                    await cur.execute(
                        "DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                        "AND subject_kind = %s AND subject_key = %s",
                        (state, subject.target_kind, subject.target_name, kind, key),
                    )
                    merged: dict[str, Any] | None = None
                    seq: float | None = None
                else:
                    merged = current
                    seq = record["seq"]
                return (True, merged, seq, guarded_skipped)

            # (ii) stamp ``_trace`` under a traced attach, before the apply + validation.
            if traced_paths:
                stamp_trace(applied_ops, traced_paths, trace_stamp(origin))

            merged = apply_path_ops(current, applied_ops)
            if validate:
                _validate_document(entry.validator, merged)

            # (iii) the record UPDATE and its ``state_writes`` provenance row (touched paths =
            # each applied op's absolute path) land in ONE writable CTE: the UPDATE's returned
            # ``seq`` feeds the write row and comes back to the caller, so the two statements are
            # one round-trip.
            paths = [list(op.get("path") or []) for op in applied_ops]
            await cur.execute(
                "WITH upd AS ("
                "UPDATE state_records SET data = %s, updated_at = clock_timestamp() "
                "WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND subject_kind = %s AND subject_key = %s "
                "RETURNING extract(epoch FROM updated_at)::float8 AS seq"
                "), w AS ("
                "INSERT INTO state_writes "
                "(state, target_kind, target_name, subject_kind, subject_key, seq, at, door, actor, consumer, "
                "meta, run_id, turn_id, paths, op_id) "
                "SELECT %s, %s, %s, %s, %s, (SELECT seq FROM upd), now(), %s, %s, %s, %s, %s, %s, %s, %s"
                ") SELECT seq FROM upd",
                (
                    Jsonb(merged),
                    state,
                    subject.target_kind,
                    subject.target_name,
                    kind,
                    key,
                    state,
                    subject.target_kind,
                    subject.target_name,
                    kind,
                    key,
                    origin.door,
                    origin.actor,
                    origin.consumer,
                    Jsonb(origin.meta) if origin.meta is not None else None,
                    origin.run_id,
                    origin.turn_id,
                    Jsonb(paths),
                    op_id,
                ),
            )
            seq_row = await cur.fetchone()
            seq = None if seq_row is None else seq_row["seq"]
            return (True, merged, seq, guarded_skipped)

    async def write_projected(
        self,
        state: str,
        subject: StateSubject,
        *,
        version: int,
        base_seq: float | None,
        document: dict[str, Any] | None,
        writes: Sequence[ProjectedWrite],
        conn: AsyncConnection[Any],
    ) -> tuple[bool, float | None]:
        """Write a subject's already-validated projected ``document`` if nothing moved since it was staged.

        Inside a SAVEPOINT on the caller's transaction: the declaration row ``FOR SHARE`` must
        still be at ``version``; the record (alias-resolved, locked through the upsert-lock) must
        still be at ``base_seq`` (``None`` = absent); every ledger ``op_id`` must insert fresh.
        Then the document lands (or, with no ``document``, a placeholder the lock created is
        removed) with one ``state_writes`` row per written item, all sharing the record's one new
        ``seq``. Returns ``(True, seq)``; on any mismatch the savepoint rolls back and the answer
        is ``(False, None)`` — the caller replays. No validation runs here: the document was
        validated when it was staged under ``version``.
        """
        try:
            async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                return True, await self._write_projected_on(cur, state, subject, version, base_seq, document, writes)
        except _ProjectionMovedError:
            return False, None

    async def _write_projected_on(
        self,
        cur: Any,
        state: str,
        subject: StateSubject,
        version: int,
        base_seq: float | None,
        document: dict[str, Any] | None,
        writes: Sequence[ProjectedWrite],
    ) -> float | None:
        """The compare-and-set write on the savepoint's cursor; raises :class:`_ProjectionMovedError` on a mismatch."""
        current_version, _kinds = await self._lock_declaration(cur, state, "SHARE")
        kind, key = await self._resolve_subject(cur, state, subject, check_outbox=False)
        where = (state, subject.target_kind, subject.target_name, kind, key)
        await cur.execute(
            "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data, "
            "updated_at) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, clock_timestamp()) "
            "ON CONFLICT (state, target_kind, target_name, subject_kind, subject_key) DO UPDATE SET "
            "state = EXCLUDED.state "
            "RETURNING data, extract(epoch FROM updated_at)::float8 AS seq, (xmax = 0) AS inserted",
            where,
        )
        record = await cur.fetchone()
        if record is None:
            raise AssertionError
        current_seq = None if record["inserted"] else record["seq"]
        fresh = current_version == version and current_seq == base_seq
        if not fresh or not await self._insert_ledger(cur, [w.op_id for w in writes if w.ledger]):
            raise _ProjectionMovedError
        if document is None:
            if record["inserted"]:
                await cur.execute(
                    "DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                    "AND subject_kind = %s AND subject_key = %s",
                    where,
                )
            return None
        await cur.execute(
            "UPDATE state_records SET data = %s, updated_at = clock_timestamp() "
            "WHERE state = %s AND target_kind = %s AND target_name = %s "
            "AND subject_kind = %s AND subject_key = %s "
            "RETURNING extract(epoch FROM updated_at)::float8 AS seq",
            (Jsonb(document), *where),
        )
        seq_row = await cur.fetchone()
        seq = None if seq_row is None else float(seq_row["seq"])
        for write in writes:
            if write.row:
                await self._insert_write(
                    cur,
                    state,
                    subject.target_kind,
                    subject.target_name,
                    kind,
                    key,
                    seq,
                    write.origin,
                    write.paths,
                    write.op_id,
                )
        return seq

    @staticmethod
    async def _insert_ledger(cur: Any, op_ids: list[str | None]) -> bool:
        """Insert each ``op_id`` into the idempotency ledger; ``False`` when any is already there."""
        for op_id in op_ids:
            await cur.execute(
                "INSERT INTO state_applied_ops (op_id, applied_at) VALUES (%s, now()) ON CONFLICT DO NOTHING",
                (op_id,),
            )
            if cur.rowcount == 0:
                return False
        return True

    async def locked_entry(self, state: str, *, catalog: ApplyEntrySource, conn: AsyncConnection[Any]) -> ApplyEntry:
        """The entry at the declaration version read ``FOR SHARE`` on the caller's transaction."""
        async with conn.cursor(row_factory=dict_row) as cur:
            version, _kinds = await self._lock_declaration(cur, state, "SHARE")
            return await catalog.write_entry(cur, state, version)

    async def erase_subject(self, state: str, subject: StateSubject, *, origin: CompletedOrigin) -> None:
        """The RTBF delete — idempotent, ALIAS-AWARE, and audited.

        The key resolves to its canonical, the surviving record dies with every
        alias pointing at it, and the erase is recorded (paths ``[[]]``). ONE txn
        under the declaration ``FOR SHARE`` lock; an undeclared state falls through
        to a plain delete.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR SHARE", (state,))
            declared = await cur.fetchone() is not None
            if not declared:
                await cur.execute(
                    "DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                    "AND subject_kind = %s AND subject_key = %s",
                    (state, *_subject_cols(subject)),
                )
                return
            kind, key = await self._resolve_subject(cur, state, subject, check_outbox=True)
            await cur.execute(
                "DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND subject_kind = %s AND subject_key = %s",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            deleted = cur.rowcount
            await cur.execute(
                "DELETE FROM state_subject_aliases WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND canonical_kind = %s AND canonical_key = %s",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            if deleted:
                await self._insert_write(
                    cur, state, subject.target_kind, subject.target_name, kind, key, None, origin, [[]], None
                )

    async def fold_subject(
        self,
        state: str,
        subject: StateSubject,
        into: StateSubject,
        mode: str,
        *,
        origin: CompletedOrigin,
        catalog: ApplyEntrySource,
    ) -> dict[str, Any]:
        """Fold ``subject`` into ``into`` in ONE txn under the declaration row ``FOR UPDATE``.

        Every ``apply_ops``'s ``FOR SHARE`` blocks, so no write lands mid-fold.
        Both keys resolve first. ``switch`` drops the subject's record; ``merge``
        folds it into the survivor (survivor wins). Refusals raise
        :class:`SubjectFoldError`; a retried fold is a quiet no-op. Records one
        write row for the survivor.
        """
        if subject.target_kind != into.target_kind or subject.target_name != into.target_name:
            raise SubjectFoldError(
                f"cannot fold across targets: {subject.target_kind}/{subject.target_name} into "
                f"{into.target_kind}/{into.target_name}"
            )
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            version, _subject_kinds = await self._lock_declaration(cur, state, "UPDATE")
            entry = await catalog.write_entry(cur, state, version)

            tk, tn = subject.target_kind, subject.target_name
            await cur.execute(
                "SELECT canonical_kind, canonical_key FROM state_subject_aliases "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND alias_kind = %s AND alias_key = %s",
                (state, tk, tn, subject.kind, subject.key),
            )
            existing = await cur.fetchone()
            target_kind, target_key = await self._resolve_subject(cur, state, into, check_outbox=False)
            # The declaration ``FOR UPDATE`` blocks every apply; a pending save enqueued since the
            # caller drained both subjects would apply under keys this fold rewrites.
            canonical_into = StateSubject(target_kind=tk, target_name=tn, kind=target_kind, key=target_key)
            await cur.execute(
                "SELECT EXISTS (SELECT 1 FROM state_outbox WHERE record_keys && %s::text[] "
                "AND (status = 'pending' OR (status = 'failed' AND records_applied_at IS NULL))) AS pending",
                ([record_key(state, s) for s in (subject, into, canonical_into)],),
            )
            pending_row = await cur.fetchone()
            if pending_row is not None and pending_row["pending"]:
                raise SubjectFoldError(f"subject {subject.kind}/{subject.key} has a pending save; retry the fold")

            from_view = {"kind": subject.kind, "key": subject.key}
            into_view = {"kind": target_kind, "key": target_key}
            if existing is not None:
                if existing["canonical_kind"] == target_kind and existing["canonical_key"] == target_key:
                    return {"mode": mode, "from": from_view, "into": into_view, "already": True, "flattened": 0}
                raise SubjectFoldError(
                    f"subject {subject.kind}/{subject.key} of state {state!r} is already folded into "
                    f"{existing['canonical_kind']}/{existing['canonical_key']}; folding it into "
                    f"{target_kind}/{target_key} would fork its identity"
                )
            if target_kind == subject.kind and target_key == subject.key:
                raise SubjectFoldError(
                    f"cannot fold subject {subject.kind}/{subject.key} of state {state!r} into itself"
                )

            await cur.execute(
                "SELECT data FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND subject_kind = %s AND subject_key = %s",
                (state, tk, tn, subject.kind, subject.key),
            )
            src_row = await cur.fetchone()

            report: dict[str, Any] = {"mode": mode, "from": from_view, "into": into_view, "already": False}
            survivor_seq: float | None = None
            if mode == "merge" and src_row is not None:
                await cur.execute(
                    "SELECT data FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                    "AND subject_kind = %s AND subject_key = %s",
                    (state, tk, tn, target_kind, target_key),
                )
                dst_row = await cur.fetchone()
                dst_data: dict[str, Any] = {} if dst_row is None else dst_row["data"]
                merged = {**src_row["data"], **dst_data}  # survivor wins; old fills absent members
                try:
                    _validate_document(entry.validator, merged)
                except Exception as exc:
                    raise SubjectFoldError(
                        f"merging subject {subject.kind}/{subject.key} into {target_kind}/{target_key} would leave an "
                        f"invalid document: {exc}"
                    ) from exc
                report["merged_members"] = sorted(k for k in src_row["data"] if k not in dst_data)
                await cur.execute(
                    "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data, "
                    "updated_at) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp()) "
                    "ON CONFLICT (state, target_kind, target_name, subject_kind, subject_key) DO UPDATE SET "
                    "data = EXCLUDED.data, updated_at = clock_timestamp() "
                    "RETURNING extract(epoch FROM updated_at)::float8 AS seq",
                    (state, tk, tn, target_kind, target_key, Jsonb(merged)),
                )
                seq_row = await cur.fetchone()
                survivor_seq = None if seq_row is None else seq_row["seq"]
            elif mode == "merge":
                report["merged_members"] = []

            if src_row is not None:
                await cur.execute(
                    "DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                    "AND subject_kind = %s AND subject_key = %s",
                    (state, tk, tn, subject.kind, subject.key),
                )
            await cur.execute(
                "INSERT INTO state_subject_aliases (state, target_kind, target_name, alias_kind, alias_key, "
                "canonical_kind, canonical_key, mode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (state, tk, tn, subject.kind, subject.key, target_kind, target_key, mode),
            )
            await cur.execute(
                "UPDATE state_subject_aliases SET canonical_kind = %s, canonical_key = %s "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND canonical_kind = %s "
                "AND canonical_key = %s",
                (target_kind, target_key, state, tk, tn, subject.kind, subject.key),
            )
            report["flattened"] = cur.rowcount
            await self._insert_write(cur, state, tk, tn, target_kind, target_key, survivor_seq, origin, [[]], None)
            return report
