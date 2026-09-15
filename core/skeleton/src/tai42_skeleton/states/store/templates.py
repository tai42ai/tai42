"""The ``state_templates`` table — template document reads, upsert, and delete."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .connection import _pool, _settings


class _TemplateStore:
    """The ``state_templates`` table."""

    async def get_template(self, name: str) -> dict[str, Any] | None:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT name, body, shipped_hash, updated_at FROM state_templates WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()

    async def list_templates(self) -> list[dict[str, Any]]:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name, body, shipped_hash, updated_at FROM state_templates ORDER BY name")
            return list(await cur.fetchall())

    async def attached_template_counts(self) -> dict[str, int]:
        """The number of states each template is attached on, keyed by template name.

        One aggregate over the attachments table for the whole catalog. A template with no
        attach is absent from the map (the caller reads a missing key as zero).
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT template, count(*) AS n FROM state_attachments GROUP BY template")
            return {row["template"]: int(row["n"]) for row in await cur.fetchall()}

    async def upsert_template(self, name: str, body: dict[str, Any], shipped_hash: str | None) -> None:
        """Write a template document.

        ``shipped_hash`` is the seed applier's canonical-body hash on a shipped default (NULL
        for an operator upload); it is the only field the applier uses to tell an unedited
        shipped template from an operator-owned one.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "INSERT INTO state_templates (name, body, shipped_hash, updated_at) VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (name) DO UPDATE SET body = EXCLUDED.body, "
                "shipped_hash = EXCLUDED.shipped_hash, updated_at = now()",
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
