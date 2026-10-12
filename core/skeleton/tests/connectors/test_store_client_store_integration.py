"""The client store on a REAL Postgres: the baseline table, its constraints, the lookup of a new flow, the
writes, the backup round-trip in each import mode and the key-rotation sweep through the store's own methods.

OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres. Without
the opt-in it SKIPS VISIBLY.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import LiteralString

import pytest
from cryptography.exceptions import InvalidTag
from psycopg.errors import CheckViolation, UniqueViolation
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.oauth.registration import RegisteredClient
from tai42_skeleton.connectors.store.backup import export_connector_oauth_clients, import_connector_oauth_clients
from tai42_skeleton.connectors.store.client_store import ConnectorClientStore, client_aad, open_row
from tai42_skeleton.connectors.store.reencrypt import reencrypt_connection_tokens
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry

from .conftest import TEST_KEK_B64

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"
ISSUER = "https://as.example.com"
OTHER = "https://other-as.example.com"
CALLBACK = "https://app.example.com/oauth-bridge.html"


async def _pg_exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@pytest.fixture
async def provider() -> AsyncIterator[str]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"the real-Postgres client store suite is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres (needs the real table, its constraints and bytea)"
        )
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    token = f"prov-it-{uuid.uuid4().hex[:12]}"
    yield token
    await _pg_exec("DELETE FROM connector_oauth_clients WHERE provider_id LIKE %s", (f"{token}%",))
    await shutdown_all_clients()


def _registered(client_id: str, *, redirect_uri: str = CALLBACK, expires: datetime | None = None) -> RegisteredClient:
    return RegisteredClient(
        client_id=client_id,
        client_secret=f"secret-{client_id}",
        auth_method="client_secret_basic",
        secret_expires_at=expires,
        redirect_uri=redirect_uri,
    )


async def test_the_lookup_order_of_a_new_flow(provider: str) -> None:
    store = ConnectorClientStore()
    assert await store.find_for_flow(provider, ISSUER, CALLBACK) is None
    old = await store.add_dynamic(provider, ISSUER, _registered("dyn-old"))
    new = await store.add_dynamic(provider, ISSUER, _registered("dyn-new"))
    await store.add_dynamic(provider, ISSUER, _registered("dyn-elsewhere", redirect_uri="https://b.example.com/cb"))
    await store.add_dynamic(provider, ISSUER, _registered("dyn-expired", expires=datetime.now(UTC) - timedelta(1)))
    await store.add_dynamic(provider, OTHER, _registered("dyn-other"))

    found = await store.find_for_flow(provider, ISSUER, CALLBACK)
    assert found is not None
    assert (found.client_id, found.client_secret, found.source) == ("dyn-new", "secret-dyn-new", "dynamic")

    assert await store.mark_dead(new.client_row_id) is True
    found = await store.find_for_flow(provider, ISSUER, CALLBACK)
    assert found is not None
    assert found.client_id == old.client_id

    await store.put_manual(provider, ISSUER, "app", "app-secret", "client_secret_post")
    found = await store.find_for_flow(provider, ISSUER, CALLBACK)
    assert found is not None
    assert (found.client_id, found.source, found.auth_method) == ("app", "manual", "client_secret_post")
    other = await store.find_for_flow(provider, OTHER, CALLBACK)
    assert other is not None
    assert other.client_id == "dyn-other"


async def test_get_returns_a_dead_row_flagged_and_only_for_its_issuer(provider: str) -> None:
    store = ConnectorClientStore()
    row = await store.add_dynamic(provider, ISSUER, _registered("dyn"))
    await store.mark_dead(row.client_row_id)
    found = await store.get(provider, ISSUER, "dyn")
    assert found is not None
    assert found.dead
    assert await store.get(provider, OTHER, "dyn") is None
    assert await store.get(provider, ISSUER, "missing") is None


async def test_put_manual_replaces_the_providers_manual_row(provider: str) -> None:
    store = ConnectorClientStore()
    first = await store.put_manual(provider, ISSUER, "app-1", "s1", "client_secret_post")
    second = await store.put_manual(provider, OTHER, "app-2", None, "none")
    assert first.client_row_id != second.client_row_id
    assert await store.manual_issuer(provider) == OTHER
    rows = [row for row in await store.enumerate_rows() if row.provider_id == provider]
    assert [(row.client_id, row.source) for row in rows] == [("app-2", "manual")]
    assert open_row(rows[0]).client_secret is None


async def test_a_manual_row_is_never_marked_dead(provider: str) -> None:
    store = ConnectorClientStore()
    manual = await store.put_manual(provider, ISSUER, "app", "s", "client_secret_post")
    assert await store.mark_dead(manual.client_row_id) is False
    found = await store.get(provider, ISSUER, "app")
    assert found is not None
    assert not found.dead


async def test_the_table_allows_one_manual_row_per_provider(provider: str) -> None:
    store = ConnectorClientStore()
    await store.put_manual(provider, ISSUER, "app-1", "s", "client_secret_post")
    with pytest.raises(UniqueViolation):
        await _pg_exec(
            "INSERT INTO connector_oauth_clients "
            "(client_row_id, provider_id, issuer, client_id, source, encrypted_blob) "
            "VALUES (%s, %s, %s, 'app-2', 'manual', %s)",
            (uuid.uuid4(), provider, OTHER, b"x"),
        )
    with pytest.raises(CheckViolation):
        await _pg_exec(
            "INSERT INTO connector_oauth_clients "
            "(client_row_id, provider_id, issuer, client_id, source, encrypted_blob) "
            "VALUES (%s, %s, %s, 'c', 'other', %s)",
            (uuid.uuid4(), provider, ISSUER, b"x"),
        )
    # The natural key holds across sources.
    await store.add_dynamic(provider, ISSUER, _registered("dyn"))
    with pytest.raises(UniqueViolation):
        await store.add_dynamic(provider, ISSUER, _registered("dyn"))


async def test_has_rows_sources_and_delete_for_provider(provider: str) -> None:
    store = ConnectorClientStore()
    manual_provider, dynamic_provider, dead_provider = f"{provider}-m", f"{provider}-d", f"{provider}-x"
    await store.put_manual(manual_provider, ISSUER, "app", "s", "client_secret_post")
    await store.add_dynamic(manual_provider, ISSUER, _registered("dyn-m"))
    await store.add_dynamic(dynamic_provider, ISSUER, _registered("dyn-d"))
    dead = await store.add_dynamic(dead_provider, ISSUER, _registered("dyn-x"))
    await store.mark_dead(dead.client_row_id)

    assert await store.sources([manual_provider, dynamic_provider, dead_provider, f"{provider}-none"]) == {
        manual_provider: "manual",
        dynamic_provider: "dynamic",
    }
    assert await store.has_rows(dead_provider) is True
    assert await store.has_rows(f"{provider}-none") is False
    assert await store.delete_for_provider(manual_provider) == 2
    assert await store.has_rows(manual_provider) is False
    assert await store.delete_for_provider(manual_provider) == 0


async def test_a_blob_moved_to_another_row_fails_to_decrypt(provider: str) -> None:
    store = ConnectorClientStore()
    first = await store.add_dynamic(provider, ISSUER, _registered("one"))
    second = await store.add_dynamic(provider, ISSUER, _registered("two"))
    first_row = await store.get_row(first.client_row_id)
    assert first_row is not None
    await _pg_exec(
        "UPDATE connector_oauth_clients SET encrypted_blob = %s WHERE client_row_id = %s",
        (first_row.encrypted_blob, uuid.UUID(second.client_row_id)),
    )
    with pytest.raises(InvalidTag):
        await store.get(provider, ISSUER, "two")


async def test_rewrite_blob_is_a_compare_and_set(provider: str) -> None:
    store = ConnectorClientStore()
    added = await store.add_dynamic(provider, ISSUER, _registered("dyn"))
    row = await store.get_row(added.client_row_id)
    assert row is not None
    assert await store.rewrite_blob(row.client_row_id, b"new", expected_blob=b"stale") is False
    assert await store.rewrite_blob(row.client_row_id, b"new", expected_blob=row.encrypted_blob) is True
    reread = await store.get_row(row.client_row_id)
    assert reread is not None
    assert reread.encrypted_blob == b"new"
    assert await store.rewrite_blob(str(uuid.uuid4()), b"x", expected_blob=b"new") is False
    assert await store.get_row(str(uuid.uuid4())) is None


async def test_restore_outcomes_in_each_mode(provider: str) -> None:
    store = ConnectorClientStore()
    kept = await store.add_dynamic(provider, ISSUER, _registered("dyn"))
    row = await store.get_row(kept.client_row_id)
    assert row is not None
    brand_new = replace(row, client_row_id=str(uuid.uuid4()), client_id="dyn-2")

    skipped = await store.restore([row, brand_new], overwrite=False)
    assert [r.outcome for r in skipped] == ["skipped_existing", "created"]

    # A row with the same natural key under another row id replaces the matched row whole.
    other_id = replace(row, client_row_id=str(uuid.uuid4()), dead=True)
    replaced = await store.restore([other_id], overwrite=True)
    assert [r.outcome for r in replaced] == ["replaced"]
    assert await store.get_row(kept.client_row_id) is None
    moved = await store.get_row(other_id.client_row_id)
    assert moved is not None
    assert moved.dead


async def test_backup_round_trip_carries_rows_verbatim(provider: str) -> None:
    store = ConnectorClientStore()
    manual = await store.put_manual(provider, ISSUER, "app", "app-secret", "client_secret_post")
    dynamic = await store.add_dynamic(provider, ISSUER, _registered("dyn", expires=datetime(2030, 1, 1, tzinfo=UTC)))
    await store.mark_dead(dynamic.client_row_id)
    before = {row.client_row_id: row for row in await store.enumerate_rows() if row.provider_id == provider}

    exported = [entry for entry in await export_connector_oauth_clients() if entry["provider_id"] == provider]
    assert {entry["client_row_id"] for entry in exported} == {manual.client_row_id, dynamic.client_row_id}
    for entry in exported:
        # Sealed credentials travel as they stand, never decrypted.
        assert base64.b64decode(entry["encrypted_blob_b64"]) == before[entry["client_row_id"]].encrypted_blob
        assert "app-secret" not in str(entry)

    await store.delete_for_provider(provider)
    report = await import_connector_oauth_clients(exported)
    assert (report.created, report.updated, report.errors) == (2, 0, [])
    after = {row.client_row_id: row for row in await store.enumerate_rows() if row.provider_id == provider}
    assert after == before
    found = await store.get(provider, ISSUER, "app")
    assert found is not None
    assert found.client_secret == "app-secret"


@pytest.mark.parametrize("mode", ["skip", "overwrite"])
async def test_an_imported_manual_row_over_a_providers_different_manual_row(provider: str, mode: str) -> None:
    store = ConnectorClientStore()
    imported_manual = await store.put_manual(provider, ISSUER, "imported-app", "imported", "client_secret_post")
    exported = [entry for entry in await export_connector_oauth_clients() if entry["provider_id"] == provider]
    await store.put_manual(provider, OTHER, "current-app", "current", "client_secret_basic")

    report = await import_connector_oauth_clients(exported, mode=mode)  # type: ignore[arg-type]
    manual_ids = [row.client_id for row in await store.enumerate_rows() if row.provider_id == provider]
    if mode == "skip":
        assert report.details["skipped_existing"] == 1
        assert (report.created, report.updated) == (0, 0)
        assert manual_ids == ["current-app"]
    else:
        assert (report.created, report.updated, report.details["skipped_existing"]) == (0, 1, 0)
        assert manual_ids == ["imported-app"]
        assert await store.manual_issuer(provider) == ISSUER
        found = await store.get(provider, ISSUER, "imported-app")
        assert found is not None
        assert found.client_row_id == imported_manual.client_row_id


async def test_the_rotation_sweep_rewrites_client_rows_through_the_store(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConnectorClientStore()
    stale = await store.add_dynamic(provider, ISSUER, _registered("dyn"))
    stale_row = await store.get_row(stale.client_row_id)
    assert stale_row is not None

    new_kek = base64.b64encode(bytes(range(100, 132))).decode("ascii")
    monkeypatch.setenv("CONNECTORS_KEK", new_kek)
    monkeypatch.setenv("CONNECTORS_KEK_PREVIOUS", TEST_KEK_B64)
    reset_all_settings()

    report = await reencrypt_connection_tokens()
    assert stale.client_row_id not in report["failed_client_row_ids"]
    assert report["clients_reencrypted"] >= 1
    row = await store.get_row(stale.client_row_id)
    assert row is not None
    assert row.encrypted_blob != stale_row.encrypted_blob
    # Under the new key alone, the rewritten blob opens with its own row's associated data.
    monkeypatch.delenv("CONNECTORS_KEK_PREVIOUS")
    reset_all_settings()
    _, under_current = crypto.decrypt_reporting_key(row.encrypted_blob, aad=client_aad(row.client_row_id))
    assert under_current
