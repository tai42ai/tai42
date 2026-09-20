"""The ``state_attachments`` table — attach reads and the effective-schema-recomposing writes."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tai42_contract.states.errors import StateNotFoundError

from .base import _StoreBase
from .connection import _pool, _settings


class _AttachmentStore(_StoreBase):
    """The ``state_attachments`` table."""

    async def get_attachment(self, state: str, template: str) -> dict[str, Any] | None:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT state, template, path, parameters, declarations, updated_at "
                "FROM state_attachments WHERE state = %s AND template = %s",
                (state, template),
            )
            return await cur.fetchone()

    async def list_attachments_for_state(self, state: str) -> list[dict[str, Any]]:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT state, template, path, parameters, declarations, updated_at "
                "FROM state_attachments WHERE state = %s ORDER BY template",
                (state,),
            )
            return list(await cur.fetchall())

    async def list_attachments_of_template(self, template: str) -> list[dict[str, Any]]:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT state, template, path, parameters, declarations, updated_at "
                "FROM state_attachments WHERE template = %s ORDER BY state",
                (template,),
            )
            return list(await cur.fetchall())

    async def list_all_attachments(self) -> list[dict[str, Any]]:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT state, template, path, parameters, declarations, updated_at "
                "FROM state_attachments ORDER BY state, template"
            )
            return list(await cur.fetchall())

    async def upsert_attachment(
        self,
        state: str,
        template: str,
        path: list[str],
        parameters: dict[str, Any],
        declarations: dict[str, Any],
        *,
        effective_schema: dict[str, Any],
        conn: AsyncConnection[Any] | None = None,
    ) -> None:
        """Write a attach row and the state's recomposed effective schema in ONE txn.

        Runs under the declaration lock (serializing against every schema change, so the
        effective schema a concurrent write validates against is never half-composed).
        Refuses loudly when the state is not declared. With ``conn`` the write joins the
        caller's transaction (a reconciler's writes commit or roll back with the attach).
        """
        async with self._write_cursor(conn) as cur:
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            if await cur.fetchone() is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            await cur.execute(
                "INSERT INTO state_attachments (state, template, path, parameters, declarations, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (state, template) DO UPDATE SET path = EXCLUDED.path, "
                "parameters = EXCLUDED.parameters, declarations = EXCLUDED.declarations, updated_at = now()",
                (state, template, Jsonb(path), Jsonb(parameters), Jsonb(declarations)),
            )
            await cur.execute(
                "UPDATE state_declarations SET effective_schema = %s, updated_at = now() WHERE name = %s",
                (Jsonb(effective_schema), state),
            )

    async def update_attachment_declarations(
        self,
        state: str,
        template: str,
        declarations: dict[str, Any],
        *,
        effective_schema: dict[str, Any],
        conn: AsyncConnection[Any] | None = None,
    ) -> bool:
        """Rewrite a attach's declarations (values only) and the state's effective schema in ONE txn.

        Runs under the declaration lock. ``False`` when no such attach exists. With
        ``conn`` the write joins the caller's transaction.
        """
        async with self._write_cursor(conn) as cur:
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            if await cur.fetchone() is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            await cur.execute(
                "UPDATE state_attachments SET declarations = %s, updated_at = now() WHERE state = %s AND template = %s",
                (Jsonb(declarations), state, template),
            )
            if cur.rowcount == 0:
                return False
            await cur.execute(
                "UPDATE state_declarations SET effective_schema = %s, updated_at = now() WHERE name = %s",
                (Jsonb(effective_schema), state),
            )
            return True

    async def update_attachment_parameters(
        self, state: str, template: str, parameters: dict[str, Any], *, effective_schema: dict[str, Any]
    ) -> bool:
        """Rewrite a attach's stored (effective) parameters and the state's effective schema in ONE txn.

        Runs under the declaration lock — used by a template replace to backfill a newly
        defaulted parameter into a live attach. ``False`` when no such attach exists.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            if await cur.fetchone() is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            await cur.execute(
                "UPDATE state_attachments SET parameters = %s, updated_at = now() WHERE state = %s AND template = %s",
                (Jsonb(parameters), state, template),
            )
            if cur.rowcount == 0:
                return False
            await cur.execute(
                "UPDATE state_declarations SET effective_schema = %s, updated_at = now() WHERE name = %s",
                (Jsonb(effective_schema), state),
            )
            return True

    async def delete_attachment(self, state: str, template: str, *, effective_schema: dict[str, Any]) -> bool:
        """Delete a attach row and rewrite the state's effective schema, in ONE txn under the declaration lock.

        ``False`` when no such attach exists. A consumer's bindings are DERIVED (never
        stored), so a attach delete cleans up nothing else.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            if await cur.fetchone() is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            await cur.execute("DELETE FROM state_attachments WHERE state = %s AND template = %s", (state, template))
            if cur.rowcount == 0:
                return False
            await cur.execute(
                "UPDATE state_declarations SET effective_schema = %s, updated_at = now() WHERE name = %s",
                (Jsonb(effective_schema), state),
            )
            return True
