"""The ``state_templates`` table — template document reads, the version probe, upsert, and delete.

Every write to a template row draws its ``version`` from the ``state_catalog_versions`` sequence in the
statement that changes it, so a version never repeats for a name, even across a delete and a re-create.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tai42_kit.clients.impl.postgres import read_connection

from .base import _StoreBase
from .connection import _pool, _settings


class _TemplateStore(_StoreBase):
    """The ``state_templates`` table."""

    async def get_template(self, name: str) -> dict[str, Any] | None:
        async with (
            _pool(_settings()) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT name, body, shipped_hash, updated_at, version FROM state_templates WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()

    async def list_templates(self) -> list[dict[str, Any]]:
        async with (
            _pool(_settings()) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name, body, shipped_hash, updated_at, version FROM state_templates ORDER BY name")
            return list(await cur.fetchall())

    async def template_version(self, name: str) -> int | None:
        """The template row's ``version``, or ``None`` when no template ``name`` is stored — the catalog probe."""
        async with self._read_cursor(None) as cur:
            await cur.execute("SELECT version FROM state_templates WHERE name = %s", (name,))
            row = await cur.fetchone()
            return None if row is None else int(row["version"])

    async def attached_template_counts(self) -> dict[str, int]:
        """The number of states each template is attached on, keyed by template name.

        One aggregate over the attachments table for the whole catalog. A template with no
        attach is absent from the map (the caller reads a missing key as zero).
        """
        async with (
            _pool(_settings()) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT template, count(*) AS n FROM state_attachments GROUP BY template")
            return {row["template"]: int(row["n"]) for row in await cur.fetchall()}

    async def upsert_template(
        self, name: str, body: dict[str, Any], shipped_hash: str | None, *, conn: AsyncConnection[Any] | None = None
    ) -> None:
        """Write a template document.

        ``shipped_hash`` is the seed applier's canonical-body hash on a shipped default (NULL
        for an operator upload); it is the only field the applier uses to tell an unedited
        shipped template from an operator-owned one. With ``conn`` the write joins the caller's
        transaction (a template replace bumps every attached declaration in the same one).
        """
        async with self._write_cursor(conn) as cur:
            await cur.execute(
                "INSERT INTO state_templates (name, body, shipped_hash, updated_at) VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (name) DO UPDATE SET body = EXCLUDED.body, "
                "shipped_hash = EXCLUDED.shipped_hash, updated_at = now(), "
                "version = nextval('state_catalog_versions')",
                (name, Jsonb(body), shipped_hash),
            )

    async def delete_template(self, name: str) -> bool:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("DELETE FROM state_templates WHERE name = %s", (name,))
            return cur.rowcount > 0
