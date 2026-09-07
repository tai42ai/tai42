"""A REAL Postgres exercise of the kit's LangGraph long-term store factory:
put/get/search/delete and namespace listing on the ``AsyncPostgresStore`` the kit
builds over a psycopg pool, with the store's own ``setup()`` migrations applied by
``create_store_resource``.

This needs real Postgres semantics (the store's jsonb tables and migration chain);
there is no fake here. It is OPT-IN: set ``TAI42_KIT_REAL_PG=1`` and point the
``TAI_DATABASE_DEFAULT_PG_*`` env at a live Postgres. Without the opt-in the test
SKIPS VISIBLY with a clear reason (never a silent skip)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langgraph.store.postgres")

from tai42_kit.db import database_settings
from tai42_kit.llm.store.store import create_store_resource, get_store_from_resource

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_KIT_REAL_PG"


@pytest.fixture
async def real_store() -> AsyncIterator[tuple[Any, tuple[str, str]]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres kit store test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs the store's real "
            "jsonb tables + migration chain — no fake)"
        )
    # The DSN comes from the kit's own settings under the DEFAULT database prefix
    # (TAI_DATABASE_DEFAULT_PG_*); create_store_resource opens the pool and runs
    # the store's setup() migrations.
    dsn = database_settings("default").pg_dsn
    pool, closer = await create_store_resource("postgres", dsn)
    try:
        store = get_store_from_resource("postgres", pool)
        # A per-run unique namespace root keeps the shared store tables collision-free.
        yield store, ("kit_it", uuid.uuid4().hex[:12])
    finally:
        await closer()


async def test_put_get_delete_roundtrip(real_store: tuple[Any, tuple[str, str]]) -> None:
    store, root = real_store
    ns = (*root, "records")
    await store.aput(ns, "k1", {"n": 1, "label": "alpha"})

    item = await store.aget(ns, "k1")
    assert item is not None
    assert item.key == "k1"
    assert item.namespace == ns
    assert item.value == {"n": 1, "label": "alpha"}

    await store.adelete(ns, "k1")
    assert await store.aget(ns, "k1") is None


async def test_search_filters_by_value(real_store: tuple[Any, tuple[str, str]]) -> None:
    store, root = real_store
    ns = (*root, "docs")
    await store.aput(ns, "k1", {"color": "red", "n": 1})
    await store.aput(ns, "k2", {"color": "blue", "n": 2})
    await store.aput(ns, "k3", {"color": "red", "n": 3})

    reds = await store.asearch(ns, filter={"color": "red"}, limit=10)
    assert {r.key for r in reds} == {"k1", "k3"}

    blues = await store.asearch(ns, filter={"color": "blue"}, limit=10)
    assert {r.key for r in blues} == {"k2"}


async def test_list_namespaces_under_prefix(real_store: tuple[Any, tuple[str, str]]) -> None:
    store, root = real_store
    await store.aput((*root, "a"), "k", {"x": 1})
    await store.aput((*root, "b"), "k", {"x": 1})
    await store.aput((*root, "b", "deep"), "k", {"x": 1})

    namespaces = await store.alist_namespaces(prefix=root, limit=100)
    assert (*root, "a") in namespaces
    assert (*root, "b") in namespaces
    assert (*root, "b", "deep") in namespaces
