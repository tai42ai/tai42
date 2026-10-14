"""Concurrent first builds of the Postgres checkpoint resource and store against a fresh database.

Every builder opens its own dedicated pool, so the builds run on separate Postgres sessions, as
separate processes booting at once do. Each round builds on a database no builder has touched;
every builder must come back with a ready resource. Opt-in: ``TAI42_KIT_REAL_PG=1`` and the
``TAI_DATABASE_DEFAULT_PG_*`` env.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

pytest.importorskip("langgraph.checkpoint.postgres.aio")
pytest.importorskip("langgraph.store.postgres")

from tai42_kit.llm.checkpoint.checkpoint import create_checkpoint_resource, get_saver_from_resource
from tai42_kit.llm.store.store import create_store_resource, get_store_from_resource

pytestmark = pytest.mark.integration

_BUILDERS = 8
_ROUNDS = 5

FreshDatabase = Callable[[], Awaitable[str]]


@pytest.fixture
async def fresh_database() -> AsyncIterator[FreshDatabase]:
    if os.environ.get("TAI42_KIT_REAL_PG") not in ("1", "true", "True"):
        pytest.skip("real-Postgres first-build test is opt-in: set TAI42_KIT_REAL_PG=1 and the PG env")
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from tai42_kit.db import database_settings

    admin_dsn = database_settings("default").pg_dsn
    created: list[str] = []

    async def make() -> str:
        name = f"tai42_kit_first_build_{uuid.uuid4().hex[:12]}"
        async with await AsyncConnection.connect(admin_dsn, autocommit=True) as admin:
            await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        info = conninfo_to_dict(admin_dsn)
        info["dbname"] = name
        return make_conninfo("", **info)

    try:
        yield make
    finally:
        async with await AsyncConnection.connect(admin_dsn, autocommit=True) as admin:
            for name in created:
                await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _build_all(build: Callable[[], Awaitable[tuple[Any, Any]]]) -> list[tuple[Any, Any]]:
    results = await asyncio.gather(*(build() for _ in range(_BUILDERS)), return_exceptions=True)
    built = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    try:
        assert errors == [], f"{len(errors)} of {_BUILDERS} concurrent first builds raised: {errors!r}"
    except AssertionError:
        await asyncio.gather(*(cleanup() for _resource, cleanup in built))
        raise
    return built


async def test_concurrent_first_checkpoint_builds_all_come_back_ready(fresh_database: FreshDatabase) -> None:
    for _ in range(_ROUNDS):
        dsn = await fresh_database()
        built = await _build_all(lambda dsn=dsn: create_checkpoint_resource("postgres", dsn))
        try:
            for resource, _cleanup in built:
                saver = get_saver_from_resource("postgres", resource)
                assert await saver.aget_tuple({"configurable": {"thread_id": "t", "checkpoint_ns": ""}}) is None
        finally:
            await asyncio.gather(*(cleanup() for _resource, cleanup in built))


async def test_concurrent_first_store_builds_all_come_back_ready(fresh_database: FreshDatabase) -> None:
    for _ in range(_ROUNDS):
        dsn = await fresh_database()
        built = await _build_all(lambda dsn=dsn: create_store_resource("postgres", dsn))
        try:
            for resource, _cleanup in built:
                store = get_store_from_resource("postgres", resource)
                assert await store.aget(("first_build",), "k") is None
        finally:
            await asyncio.gather(*(cleanup() for _resource, cleanup in built))
