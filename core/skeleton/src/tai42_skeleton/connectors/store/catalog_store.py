"""Loader for the connector category groupings.

The ``connector_category`` table holds one row per UI grouping for providers —
a public, secret-free display label plus its sort order. :func:`fetch_categories`
reads every row in display order; it is served alongside the provider catalog so
clients can group/order/label providers by category.

Postgres is reached through the app-pooled ``PostgresClient`` so it shares one
pool with the token store (same DSN), closed centrally at shutdown.
"""

from __future__ import annotations

from pydantic import BaseModel
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.db import SKELETON_COMPONENT


class ConnectorCategory(BaseModel):
    """One ``connector_category`` row — the UI grouping for providers."""

    id: str
    display_name: str
    sort_order: int


async def fetch_categories() -> list[ConnectorCategory]:
    """Read every ``connector_category`` row, ordered for display
    (``sort_order``, then id)."""
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT id, display_name, sort_order FROM connector_category ORDER BY sort_order, id")
        rows = await cur.fetchall()
    return [
        ConnectorCategory(id=category_id, display_name=display_name, sort_order=sort_order)
        for category_id, display_name, sort_order in rows
    ]
