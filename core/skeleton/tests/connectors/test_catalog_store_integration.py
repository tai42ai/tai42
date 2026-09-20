"""A REAL Postgres read of the connector category catalog: the baseline migration SEEDS
the ``connector_category`` rows, and :func:`fetch_categories` returns them in genuine
``ORDER BY sort_order, id`` display order — with ``other`` last through its large sentinel.
The offline suite fakes this seam to an empty set, so only a real database proves the
seeded rows and their SQL ordering.

It is OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a
live Postgres. Without the opt-in it SKIPS VISIBLY with a clear reason (never a silent
skip)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.db import apply_migrations
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.store.catalog_store as catalog_store
from tai42_skeleton.connectors.store.catalog_store import fetch_categories
from tai42_skeleton.db import skeleton_entry

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


@pytest.fixture
async def real_catalog(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres connector-catalog read is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs the seeded rows — no fake)"
        )
    # The suite-wide autouse fixture points the catalog store's ``client_ctx`` at an
    # empty-returning fake so the offline suite never opens Postgres; this test needs the
    # REAL seeded table, so restore the genuine pooled seam.
    monkeypatch.setattr(catalog_store, "client_ctx", client_ctx)
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    yield
    await shutdown_all_clients()


async def test_fetch_categories_returns_seeded_rows_in_display_order(real_catalog: None) -> None:
    categories = await fetch_categories()
    ids = [c.id for c in categories]
    # The six baseline-seeded categories, in real ``sort_order, id`` order.
    assert ids == ["communication", "productivity", "dev-tools", "data", "ai-ml", "other"]
    # ``other`` carries the large sentinel so it always sorts last regardless of insert order.
    assert categories[-1].id == "other"
    assert categories[-1].sort_order == 1000
    assert categories[0].display_name == "Communication"
