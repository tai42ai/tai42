"""The client store's own rules, offline: the lookup order of a new flow, the sealing of a row's credentials to
that row, and the refusal of a store whose database is not configured.

The SQL is exercised on a real Postgres in ``test_store_client_store_integration.py``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag
from tai42_contract.connectors.errors import ConnectorError
from tai42_contract.errors import ErrorKind, error_kind
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.store.client_store import (
    ConnectorClientStore,
    ConnectorStoreNotConfiguredError,
    StoredClientRow,
    client_aad,
    open_row,
    seal_credentials,
    select_for_flow,
)

ISSUER = "https://as.example.com"
OTHER = "https://other-as.example.com"
CALLBACK = "https://app.example.com/oauth-bridge.html"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _row(
    *,
    source: str = "dynamic",
    issuer: str = ISSUER,
    client_id: str = "c",
    redirect_uri: str | None = CALLBACK,
    expires: datetime | None = None,
    dead: bool = False,
    secret: str | None = "s",
) -> StoredClientRow:
    row_id = str(uuid.uuid4())
    return StoredClientRow(
        client_row_id=row_id,
        provider_id="remote",
        issuer=issuer,
        client_id=client_id,
        source=source,  # type: ignore[arg-type]
        redirect_uri=None if source == "manual" else redirect_uri,
        secret_expires_at=expires,
        dead=dead,
        encrypted_blob=seal_credentials(row_id, secret, "client_secret_basic"),
        created_at=NOW,
        updated_at=NOW,
    )


def _pick(rows: list[StoredClientRow], *, issuer: str = ISSUER, redirect_uri: str = CALLBACK) -> str | None:
    found = select_for_flow(rows, issuer=issuer, redirect_uri=redirect_uri, now=NOW)
    return None if found is None else found.client_id


# -- the lookup order of a new flow -----------------------------------------------------------


def test_the_manual_client_beats_every_dynamic_one():
    rows = [_row(client_id="dyn-new"), _row(source="manual", client_id="app"), _row(client_id="dyn-old")]
    assert _pick(rows) == "app"


def test_the_newest_usable_dynamic_client_is_chosen():
    assert _pick([_row(client_id="dyn-new"), _row(client_id="dyn-old")]) == "dyn-new"


@pytest.mark.parametrize(
    "unusable",
    [
        {"dead": True},
        {"expires": NOW - timedelta(seconds=1)},
        {"expires": NOW},
        {"redirect_uri": "https://other.example.com/oauth-bridge.html"},
    ],
    ids=["dead", "expired", "expires-now", "another-redirect-uri"],
)
def test_an_unusable_dynamic_client_is_skipped(unusable):
    rows = [_row(client_id="skip", **unusable), _row(client_id="usable")]
    assert _pick(rows) == "usable"
    assert _pick([_row(client_id="skip", **unusable)]) is None


def test_a_secret_that_expires_later_is_usable():
    assert _pick([_row(client_id="later", expires=NOW + timedelta(days=1))]) == "later"


def test_another_issuers_rows_are_never_returned():
    rows = [_row(source="manual", issuer=OTHER, client_id="app-b"), _row(issuer=OTHER, client_id="dyn-b")]
    assert _pick(rows) is None
    assert _pick(rows, issuer=OTHER) == "app-b"


def test_issuers_compare_without_normalisation():
    assert _pick([_row(source="manual", issuer=ISSUER + "/")]) is None


# -- the sealing --------------------------------------------------------------------------------


def test_a_row_opens_with_its_own_associated_data():
    row = _row(secret="top-secret")
    opened = open_row(row)
    assert (opened.client_secret, opened.auth_method, opened.client_row_id) == (
        "top-secret",
        "client_secret_basic",
        row.client_row_id,
    )
    assert "top-secret" not in repr(opened)
    assert "encrypted_blob" not in repr(row)


def test_a_blob_moved_to_another_row_fails_to_open():
    first, second = _row(), _row()
    with pytest.raises(InvalidTag):
        open_row(replace(second, encrypted_blob=first.encrypted_blob))


def test_a_client_blob_never_opens_as_a_connection_blob():
    row = _row()
    with pytest.raises(InvalidTag):
        crypto.decrypt(row.encrypted_blob, aad=row.client_row_id)
    assert client_aad(row.client_row_id) == f"oauth-client:{row.client_row_id}"


def test_a_public_client_has_no_secret():
    assert open_row(_row(secret=None)).client_secret is None


@pytest.mark.parametrize(
    "payload",
    [
        {"client_secret": 7, "token_endpoint_auth_method": "none"},
        {"client_secret": "s", "token_endpoint_auth_method": "private_key_jwt"},
        ["not", "an", "object"],
    ],
    ids=["non-string-secret", "unknown-method", "not-an-object"],
)
def test_malformed_credentials_raise(payload):
    row = _row()
    blob = crypto.encrypt(json.dumps(payload).encode(), aad=client_aad(row.client_row_id))
    with pytest.raises(ConnectorError):
        open_row(replace(row, encrypted_blob=blob))


# -- the store-off refusal ----------------------------------------------------------------------


@pytest.fixture
def store_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)
    reset_all_settings()


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("find_for_flow", ("remote", ISSUER, CALLBACK)),
        ("get", ("remote", ISSUER, "c")),
        ("manual_issuer", ("remote",)),
        ("has_rows", ("remote",)),
        ("sources", (["remote"],)),
        ("put_manual", ("remote", ISSUER, "c", "s", "client_secret_post")),
        ("mark_dead", (str(uuid.uuid4()),)),
        ("delete_for_provider", ("remote",)),
        ("enumerate_rows", ()),
        ("get_row", (str(uuid.uuid4()),)),
        ("rewrite_blob", (str(uuid.uuid4()), b"n")),
        ("restore", ([],)),
    ],
)
async def test_every_method_refuses_before_any_sql_when_the_store_is_off(store_off, method, args):
    store = ConnectorClientStore()
    kwargs = {"expected_blob": b"o"} if method == "rewrite_blob" else {}
    if method == "restore":
        kwargs = {"overwrite": False}
    with pytest.raises(ConnectorStoreNotConfiguredError) as info:
        await getattr(store, method)(*args, **kwargs)
    assert "connector client store is not configured" in str(info.value)
    assert "TAI_DATABASE_DEFAULT_PG_PASSWORD" in str(info.value)


def test_the_store_off_refusal_is_unavailable():
    assert error_kind(ConnectorStoreNotConfiguredError("x")) is ErrorKind.UNAVAILABLE


async def test_add_dynamic_refuses_when_the_store_is_off(store_off):
    from tai42_skeleton.connectors.oauth.registration import RegisteredClient

    registered = RegisteredClient(
        client_id="c", client_secret=None, auth_method="none", secret_expires_at=None, redirect_uri=CALLBACK
    )
    with pytest.raises(ConnectorStoreNotConfiguredError):
        await ConnectorClientStore().add_dynamic("remote", ISSUER, registered)


async def test_put_manual_refuses_an_unknown_method():
    with pytest.raises(ValueError, match="private_key_jwt"):
        await ConnectorClientStore().put_manual("remote", ISSUER, "c", "s", "private_key_jwt")  # type: ignore[arg-type]
