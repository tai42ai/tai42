"""A REAL Postgres exercise of the marketplace install-attribution store: the
``source IN ('pypi','github','spec')`` CHECK constraint as the durable authority, the
``ON CONFLICT (ref) DO UPDATE`` replace an update performs, the JSONB ``spec`` /
``route_mounts`` round-trip (including the column default that fills an omitted map), and
the ordered listing — SQL behavior a fake pool cannot prove.

It is OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a
live Postgres. Without the opt-in the tests SKIP VISIBLY with a clear reason (never a
silent skip)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import LiteralString

import pytest
from psycopg.errors import CheckViolation
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry
from tai42_skeleton.marketplace.store import MarketplaceInstallStore

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@pytest.fixture
async def store() -> AsyncIterator[tuple[MarketplaceInstallStore, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres marketplace store test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs the CHECK constraint + jsonb — no fake)"
        )
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    token = uuid.uuid4().hex[:12]
    await _exec("DELETE FROM marketplace_installs WHERE ref LIKE %s", (f"%{token}%",))
    yield MarketplaceInstallStore(), token
    await _exec("DELETE FROM marketplace_installs WHERE ref LIKE %s", (f"%{token}%",))
    await shutdown_all_clients()


async def test_record_and_get_round_trip_jsonb(store: tuple[MarketplaceInstallStore, str]) -> None:
    s, token = store
    ref = f"tai42/{token}-tool"
    spec = {"spec_version": 1, "name": f"{token}-tool", "provides": [{"item": "x"}]}
    await s.record(
        ref,
        "1.2.3",
        "pypi",
        None,
        None,
        None,
        None,
        spec,
        contract_version="0.1.0",
        skeleton_version="0.1.0",
        route_mounts={"x": "/base"},
    )
    record = await s.get(ref)
    assert record is not None
    # The opaque JSONB ``spec`` and ``route_mounts`` come back through the real jsonb column.
    assert record.spec == spec
    assert record.route_mounts == {"x": "/base"}
    assert record.installed_at is not None


async def test_source_check_constraint_rejects_unknown(store: tuple[MarketplaceInstallStore, str]) -> None:
    s, token = store
    # The channel vocabulary is enforced by the real ``source IN (...)`` CHECK — a bogus
    # channel is rejected by the database, not by an in-process guard.
    with pytest.raises(CheckViolation):
        await s.record(
            f"tai42/{token}-bad",
            "1.0.0",
            "ftp",
            None,
            None,
            None,
            None,
            {},
            contract_version="0.1.0",
            skeleton_version="0.1.0",
        )


async def test_on_conflict_replaces_row_in_place(store: tuple[MarketplaceInstallStore, str]) -> None:
    s, token = store
    ref = f"tai42/{token}-tool"
    await s.record(
        ref, "1.0.0", "pypi", None, None, None, None, {"v": 1}, contract_version="0.1.0", skeleton_version="0.1.0"
    )
    # The update flow re-``record``s the same ref: ``ON CONFLICT (ref) DO UPDATE`` replaces the
    # single row rather than inserting a second — a github pin now, spec swapped.
    await s.record(
        ref,
        "2.0.0",
        "github",
        "https://example.test/repo",
        "v2.0.0",
        "https://example.test/a.tgz",
        "deadbeef",
        {"v": 2},
        contract_version="0.1.0",
        skeleton_version="0.1.0",
    )
    record = await s.get(ref)
    assert record is not None
    assert (record.version, record.source, record.spec, record.sha256) == ("2.0.0", "github", {"v": 2}, "deadbeef")
    assert [r.ref for r in await s.list_installed() if token in r.ref] == [ref]


async def test_route_mounts_defaults_to_empty_when_omitted(store: tuple[MarketplaceInstallStore, str]) -> None:
    s, token = store
    ref = f"tai42/{token}-tool"
    # ``None`` writes the column default ``'{}'::jsonb`` — the boot mount-map then reproduces
    # each item's declared base.
    await s.record(ref, "1.0.0", "spec", None, None, None, None, {}, contract_version="0.1.0", skeleton_version="0.1.0")
    record = await s.get(ref)
    assert record is not None
    assert record.route_mounts == {}


async def test_list_installed_ordered_by_ref(store: tuple[MarketplaceInstallStore, str]) -> None:
    s, token = store
    for suffix in ("c", "a", "b"):
        await s.record(
            f"tai42/{token}-{suffix}",
            "1.0.0",
            "pypi",
            None,
            None,
            None,
            None,
            {},
            contract_version="0.1.0",
            skeleton_version="0.1.0",
        )
    ordered = [r.ref for r in await s.list_installed() if token in r.ref]
    assert ordered == [f"tai42/{token}-a", f"tai42/{token}-b", f"tai42/{token}-c"]
