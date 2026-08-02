"""Catalog loader: fetch_categories over a faked pooled Postgres client."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

import tai42_skeleton.connectors.store.catalog_store as catalog_store
from tai42_skeleton.connectors.store.catalog_store import ConnectorCategory, fetch_categories


class _FakeCursor:
    def __init__(self, pg: _FakePg) -> None:
        self._pg = pg
        self._rows: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        norm = " ".join(sql.split())
        self._pg.executed.append(norm)
        if "FROM connector_category" in norm:
            self._rows = self._pg.categories
        else:
            self._rows = []

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, pg):
        self._pg = pg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._pg)


class _FakePg:
    def __init__(self, *, categories=None) -> None:
        self.categories = categories or []
        self.executed: list[str] = []

    def connection(self):
        return _FakeConn(self)


@pytest.fixture
def install_pg(monkeypatch):
    def _install(*, categories=None):
        pg = _FakePg(categories=categories)

        @asynccontextmanager
        async def fake_client_ctx(client_cls, settings=None, **kwargs):
            yield pg

        monkeypatch.setattr(catalog_store, "client_ctx", fake_client_ctx)
        return pg

    return _install


# -- fetch_categories --------------------------------------------------------


async def test_fetch_categories(install_pg):
    pg = install_pg(categories=[("data", "Data", 1), ("other", "Other", 99)])
    cats = await fetch_categories()
    assert cats == [
        ConnectorCategory(id="data", display_name="Data", sort_order=1),
        ConnectorCategory(id="other", display_name="Other", sort_order=99),
    ]
    # Categories are read in display order.
    assert any("ORDER BY sort_order, id" in norm for norm in pg.executed)


async def test_fetch_categories_empty(install_pg):
    install_pg(categories=[])
    assert await fetch_categories() == []
