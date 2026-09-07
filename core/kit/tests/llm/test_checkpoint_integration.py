"""A REAL Postgres exercise of the kit's LangGraph checkpointer factory:
put/get_tuple/list on the ``AsyncPostgresSaver`` the kit builds over a psycopg
pool, with the saver's own ``setup()`` migrations applied by
``create_checkpoint_resource`` — including the thread / checkpoint-ns scoping the
unit tests only mock.

This needs real Postgres semantics (the saver's checkpoint tables and migration
chain); there is no fake here. It is OPT-IN: set ``TAI42_KIT_REAL_PG=1`` and point
the ``TAI_DATABASE_DEFAULT_PG_*`` env at a live Postgres. Without the opt-in the
test SKIPS VISIBLY with a clear reason (never a silent skip)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langgraph.checkpoint.postgres.aio")

from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint

from tai42_kit.db import database_settings
from tai42_kit.llm.checkpoint.checkpoint import create_checkpoint_resource, get_saver_from_resource

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_KIT_REAL_PG"

_METADATA: dict[str, Any] = {"source": "input", "step": 0, "parents": {}}


def _config(thread_id: str, checkpoint_ns: str = "") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}


@pytest.fixture
async def real_saver() -> AsyncIterator[Any]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres kit checkpoint test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs the saver's real "
            "checkpoint tables + migration chain — no fake)"
        )
    # The DSN comes from the kit's own settings under the DEFAULT database prefix
    # (TAI_DATABASE_DEFAULT_PG_*); create_checkpoint_resource opens the pool and
    # runs the saver's setup() migrations.
    dsn = database_settings("default").pg_dsn
    pool, closer = await create_checkpoint_resource("postgres", dsn)
    try:
        yield get_saver_from_resource("postgres", pool)
    finally:
        await closer()


async def test_put_get_tuple_roundtrip(real_saver: Any) -> None:
    thread_id = uuid.uuid4().hex
    config = _config(thread_id)
    checkpoint = empty_checkpoint()

    saved_config = await real_saver.aput(config, checkpoint, _METADATA, {})

    tup = await real_saver.aget_tuple(saved_config)
    assert tup is not None
    assert tup.checkpoint["id"] == checkpoint["id"]
    assert tup.metadata["source"] == "input"

    # A config that names only the thread + ns (no checkpoint_id) reads the latest.
    latest = await real_saver.aget_tuple(config)
    assert latest is not None
    assert latest.checkpoint["id"] == checkpoint["id"]


async def test_list_walks_the_thread_history(real_saver: Any) -> None:
    thread_id = uuid.uuid4().hex
    config = _config(thread_id)

    first = empty_checkpoint()
    config1 = await real_saver.aput(config, first, _METADATA, {})
    second = create_checkpoint(first, None, 1)
    await real_saver.aput(config1, second, {**_METADATA, "step": 1}, {})

    listed = [t async for t in real_saver.alist(config)]
    ids = {t.checkpoint["id"] for t in listed}
    assert {first["id"], second["id"]} <= ids


async def test_checkpoint_ns_scopes_a_shared_thread(real_saver: Any) -> None:
    thread_id = uuid.uuid4().hex
    config_a = _config(thread_id, "branch-a")
    config_b = _config(thread_id, "branch-b")
    checkpoint_a = empty_checkpoint()
    checkpoint_b = empty_checkpoint()

    await real_saver.aput(config_a, checkpoint_a, _METADATA, {})
    await real_saver.aput(config_b, checkpoint_b, _METADATA, {})

    tup_a = await real_saver.aget_tuple(config_a)
    tup_b = await real_saver.aget_tuple(config_b)
    assert tup_a is not None
    assert tup_a.checkpoint["id"] == checkpoint_a["id"]
    assert tup_b is not None
    assert tup_b.checkpoint["id"] == checkpoint_b["id"]

    # list scoped to one namespace never leaks the other namespace's checkpoint.
    ids_a = {t.checkpoint["id"] async for t in real_saver.alist(config_a)}
    assert checkpoint_a["id"] in ids_a
    assert checkpoint_b["id"] not in ids_a
