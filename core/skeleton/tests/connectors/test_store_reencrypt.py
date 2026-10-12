"""The KEK re-encrypt sweep: full-table enumeration (incl. expired rows), compare-and-set
write-back, skip-already-current, expiry preservation, and failure counting."""

from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.store.redis_pg as redis_pg
import tai42_skeleton.connectors.store.reencrypt as reencrypt
from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.store.client_store import open_row, seal_credentials
from tai42_skeleton.connectors.store.reencrypt import reencrypt_connection_tokens

from .client_store_fake import FakeClientStore
from .conftest import CID, CID2, TEST_KEK_B64

# A second, unrelated 32-byte key the rotation moves TO (current), keeping A as previous.
_KEY_B_B64 = base64.b64encode(bytes(range(96, 128))).decode("ascii")
# A key no ring ever holds — a blob under it can be opened by nobody.
_ORPHAN_KEY = base64.b64encode(bytes([7]) * 32).decode("ascii")


class _FakeRedis:
    """The cache seam store.put touches after a durable commit — a no-op that records."""

    def __init__(self) -> None:
        self.evals = 0

    async def eval(self, script, numkeys, *args):
        self.evals += 1
        return 1

    async def delete(self, key):
        return 1


class _FakeCursor:
    def __init__(self, pg: _FakePg) -> None:
        self._pg = pg
        self._one = None
        self._all: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        norm = " ".join(sql.split())
        if norm.startswith("SELECT connection_id, provider_id, alias, encrypted_blob, session_expires_at"):
            if "WHERE connection_id = %s" in norm:
                # The store's durable re-read of one row after a CAS miss.
                cid = str(params[0])
                row = self._pg.rows.get(cid)
                self._one = None if row is None else (uuid.UUID(cid), "acme", cid, row["blob"], row["exp"])
            else:
                # The store's full listing — EVERY row, expired included.
                self._all = [
                    (uuid.UUID(cid), "acme", cid, r["blob"], r["exp"]) for cid, r in sorted(self._pg.rows.items())
                ]
        elif norm.startswith("UPDATE"):
            # CAS: params = (new_blob, session_expires_at, conn_uuid, expected_blob).
            cid = str(params[2])
            # A queued concurrent writer wins this compare-and-set first: it rewrites (or
            # deletes) the row BEFORE the expected_blob check, so this UPDATE misses and
            # the sweep's re-read sees the peer's write.
            intercepts = self._pg.cas_intercepts.get(cid)
            if intercepts:
                intercepts.pop(0)(self._pg, cid)
            row = self._pg.rows.get(cid)
            if row is not None and row["blob"] == params[3]:
                row.update(blob=params[0], exp=params[1], ver=row["ver"] + 1)
                self._one = (row["ver"],)
            else:
                self._one = None
        else:
            raise AssertionError(f"unexpected SQL in sweep test: {norm}")

    async def fetchone(self):
        return self._one

    async def fetchall(self):
        return self._all


class _FakeConn:
    def __init__(self, pg: _FakePg) -> None:
        self._pg = pg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    autocommit = False

    async def set_autocommit(self, value: bool) -> None:
        self.autocommit = value

    def cursor(self):
        return _FakeCursor(self._pg)


class _FakePg:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        # Per-connection queues of concurrent-writer callbacks. Each entry runs on one
        # UPDATE attempt (before its expected_blob check), simulating a peer that won the
        # compare-and-set first — the way a CAS miss is produced in these tests.
        self.cas_intercepts: dict[str, list] = {}

    def connection(self):
        return _FakeConn(self)


@pytest.fixture
def sweep_fakes(monkeypatch):
    redis = _FakeRedis()
    pg = _FakePg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is RedisClient:
            yield redis
        elif client_cls is PostgresClient:
            yield pg
        else:
            raise AssertionError(f"unexpected client {client_cls!r}")

    # The sweep enumerates, re-reads and writes back through the token store.
    monkeypatch.setattr(redis_pg, "client_ctx", fake_client_ctx)
    return pg, redis


def _seed(pg: _FakePg, cid: str, blob: bytes, *, exp: datetime | None = None, ver: int = 1) -> None:
    pg.rows[cid] = {"blob": blob, "exp": exp, "ver": ver}


def _rotate_to_b(monkeypatch) -> None:
    """Rotate the current KEK to B, keeping A (the crypto_env default) as previous."""
    monkeypatch.setenv("CONNECTORS_KEK", _KEY_B_B64)
    monkeypatch.setenv("CONNECTORS_KEK_PREVIOUS", TEST_KEK_B64)
    reset_all_settings()


def _decrypts_under_b_alone(blob: bytes, connection_id: str, monkeypatch) -> bytes:
    """Decrypt with B as the ONLY ring key (previous dropped), proving convergence."""
    monkeypatch.setenv("CONNECTORS_KEK", _KEY_B_B64)
    monkeypatch.delenv("CONNECTORS_KEK_PREVIOUS", raising=False)
    reset_all_settings()
    return crypto.decrypt(blob, aad=connection_id)


async def test_sweep_reencrypts_stale_and_skips_current(sweep_fakes, monkeypatch):
    pg, _ = sweep_fakes
    stale = crypto.encrypt(b"token-1", aad=CID)  # under A (crypto_env default)
    _rotate_to_b(monkeypatch)
    current = crypto.encrypt(b"token-2", aad=CID2)  # under B (now current)
    _seed(pg, CID, stale)
    _seed(pg, CID2, current)

    result = await reencrypt_connection_tokens()

    assert result["scanned"] == 2
    assert result["reencrypted"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["failed_connection_ids"] == []
    # The stale row was rewritten (a new ciphertext) and now opens under B alone.
    assert pg.rows[CID]["blob"] != stale
    assert _decrypts_under_b_alone(pg.rows[CID]["blob"], CID, monkeypatch) == b"token-1"
    # The already-current row was left untouched.
    assert pg.rows[CID2]["blob"] == current


async def test_sweep_includes_expired_rows(sweep_fakes, monkeypatch):
    pg, _ = sweep_fakes
    stale = crypto.encrypt(b"token-exp", aad=CID)  # under A
    _rotate_to_b(monkeypatch)
    past = datetime.now(UTC) - timedelta(days=1)
    _seed(pg, CID, stale, exp=past)

    result = await reencrypt_connection_tokens()

    # An expired-but-not-purged row is covered (unlike list()), so dropping A is safe.
    assert result["scanned"] == 1
    assert result["reencrypted"] == 1
    assert result["skipped"] == 0
    # Its session expiry is preserved verbatim through the re-encrypt write-back.
    assert pg.rows[CID]["exp"] == past


async def test_sweep_counts_undecryptable_as_failed(sweep_fakes, monkeypatch):
    pg, _ = sweep_fakes
    # A blob under a key no ring holds: encrypt under an orphan current key, then rotate
    # to B/prev A so neither ring key can open it.
    monkeypatch.setenv("CONNECTORS_KEK", _ORPHAN_KEY)
    monkeypatch.delenv("CONNECTORS_KEK_PREVIOUS", raising=False)
    reset_all_settings()
    orphan = crypto.encrypt(b"unreadable", aad=CID)
    _rotate_to_b(monkeypatch)
    _seed(pg, CID, orphan)

    result = await reencrypt_connection_tokens()

    assert result["scanned"] == 1
    assert result["reencrypted"] == 0
    assert result["failed"] == 1
    assert result["failed_connection_ids"] == [CID]
    # The unreadable blob is left as-is, never a partial/lossy rewrite.
    assert pg.rows[CID]["blob"] == orphan


async def test_sweep_missing_kek_propagates(sweep_fakes, monkeypatch):
    pg, _ = sweep_fakes
    blob = crypto.encrypt(b"token", aad=CID)  # under A
    _seed(pg, CID, blob)
    # A missing KEK is a deployment fault for EVERY row — it must propagate, not be
    # counted as a per-row failure.
    monkeypatch.delenv("CONNECTORS_KEK", raising=False)
    monkeypatch.delenv("CONNECTORS_KEK_PREVIOUS", raising=False)
    reset_all_settings()
    with pytest.raises(crypto.ConnectorEncryptionConfigError):
        await reencrypt_connection_tokens()


async def test_sweep_cas_miss_reread_already_current_is_skipped(sweep_fakes, monkeypatch):
    # A concurrent refresh wins the compare-and-set first and leaves the row ALREADY under
    # the current key: the sweep re-reads, sees the peer's current-key blob, and skips —
    # counting the retry, never rewriting a converged blob.
    pg, _ = sweep_fakes
    stale = crypto.encrypt(b"token", aad=CID)  # under A
    _rotate_to_b(monkeypatch)
    peer_current = crypto.encrypt(b"token", aad=CID)  # peer already rewrote under B
    _seed(pg, CID, stale)

    def _peer_rewrote_under_current(pg_, cid):
        pg_.rows[cid]["blob"] = peer_current

    pg.cas_intercepts[CID] = [_peer_rewrote_under_current]

    result = await reencrypt_connection_tokens()

    assert result["scanned"] == 1
    assert result["reencrypted"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["cas_retries"] >= 1
    # The peer's converged blob is left verbatim — no redundant rewrite.
    assert pg.rows[CID]["blob"] == peer_current


async def test_sweep_cas_miss_reread_deleted_row_is_skipped(sweep_fakes, monkeypatch):
    # A concurrent disconnect deletes the row between the sweep's read and its write: the
    # CAS misses, the re-read finds no row, and the sweep skips it — nothing to converge.
    pg, _ = sweep_fakes
    stale = crypto.encrypt(b"token", aad=CID)  # under A
    _rotate_to_b(monkeypatch)
    _seed(pg, CID, stale)

    def _peer_deleted(pg_, cid):
        del pg_.rows[cid]

    pg.cas_intercepts[CID] = [_peer_deleted]

    result = await reencrypt_connection_tokens()

    assert result["scanned"] == 1
    assert result["reencrypted"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["cas_retries"] >= 1
    assert CID not in pg.rows


async def test_sweep_sustained_cas_contention_fails_after_max_retries(sweep_fakes, monkeypatch):
    # Sustained contention: every UPDATE loses to a peer that rewrites the row to a fresh
    # still-stale (previous-key) blob, so no CAS ever commits. The sweep exhausts its
    # bounded retry budget and reports the row as failed — never a silent give-up.
    pg, _ = sweep_fakes
    stale = crypto.encrypt(b"token-0", aad=CID)  # under A
    # Precompute distinct still-stale (A) blobs the peer rotates in on each attempt, so
    # every re-read yields a decryptable-but-not-current blob and the next CAS misses too.
    peer_stale = [crypto.encrypt(f"token-{i}".encode(), aad=CID) for i in range(1, reencrypt._MAX_CAS_RETRIES + 2)]
    _rotate_to_b(monkeypatch)
    _seed(pg, CID, stale)

    def _make_rewrite(blob):
        def _rewrite(pg_, cid):
            pg_.rows[cid]["blob"] = blob

        return _rewrite

    pg.cas_intercepts[CID] = [_make_rewrite(b) for b in peer_stale]

    result = await reencrypt_connection_tokens()

    assert result["scanned"] == 1
    assert result["reencrypted"] == 0
    assert result["skipped"] == 0
    assert result["failed"] == 1
    assert result["failed_connection_ids"] == [CID]
    # One miss per attempt across the bounded budget (initial try + _MAX_CAS_RETRIES).
    assert result["cas_retries"] == reencrypt._MAX_CAS_RETRIES + 1


# -- the stored OAuth client rows -----------------------------------------------------------


def _client_row(store: FakeClientStore):
    return store.add_row(provider_id="remote", issuer="https://as.example.com", client_id="dyn", source="dynamic")


async def test_the_sweep_rewrites_stale_client_rows_and_reports_them(sweep_fakes, monkeypatch, client_store_fake):
    stale = _client_row(client_store_fake)
    _rotate_to_b(monkeypatch)
    current = _client_row(client_store_fake)

    result = await reencrypt_connection_tokens()

    assert (result["clients_scanned"], result["clients_reencrypted"], result["clients_skipped"]) == (2, 1, 1)
    assert (result["clients_failed"], result["failed_client_row_ids"]) == (0, [])
    rewritten = client_store_fake.row(stale.client_row_id)
    assert rewritten.encrypted_blob != stale.encrypted_blob
    assert client_store_fake.row(current.client_row_id).encrypted_blob == current.encrypted_blob
    assert "rewrite_blob" in client_store_fake.calls
    monkeypatch.delenv("CONNECTORS_KEK_PREVIOUS")
    reset_all_settings()
    assert open_row(rewritten).client_secret == "stored-secret"


async def test_an_unreadable_client_row_is_counted_and_named(sweep_fakes, monkeypatch, client_store_fake):
    row = _client_row(client_store_fake)
    client_store_fake.rows[0] = replace(row, encrypted_blob=crypto.encrypt(b"x", aad="elsewhere"))

    result = await reencrypt_connection_tokens()

    assert result["clients_failed"] == 1
    assert result["failed_client_row_ids"] == [row.client_row_id]


async def test_a_client_row_changed_meanwhile_is_reread_once(sweep_fakes, monkeypatch, client_store_fake):
    stale = _client_row(client_store_fake)
    _rotate_to_b(monkeypatch)
    peer = seal_credentials(stale.client_row_id, "peer-secret", "client_secret_post")
    original = client_store_fake.rewrite_blob

    async def _peer_wins_first(client_row_id, new_blob, *, expected_blob):
        if expected_blob == stale.encrypted_blob:
            client_store_fake.rows[0] = replace(stale, encrypted_blob=peer)
            return False
        return await original(client_row_id, new_blob, expected_blob=expected_blob)

    monkeypatch.setattr(client_store_fake, "rewrite_blob", _peer_wins_first)
    result = await reencrypt_connection_tokens()
    # The peer's write is already under the current key: skipped after one re-read.
    assert (result["clients_skipped"], result["cas_retries"]) == (1, 1)
    assert client_store_fake.row(stale.client_row_id).encrypted_blob == peer


async def test_a_client_row_deleted_meanwhile_is_skipped(sweep_fakes, monkeypatch, client_store_fake):
    stale = _client_row(client_store_fake)
    _rotate_to_b(monkeypatch)

    async def _gone(client_row_id, new_blob, *, expected_blob):
        client_store_fake.rows.clear()
        return False

    monkeypatch.setattr(client_store_fake, "rewrite_blob", _gone)
    result = await reencrypt_connection_tokens()
    assert (result["clients_skipped"], result["clients_failed"]) == (1, 0)
    assert stale.client_row_id not in result["failed_client_row_ids"]


async def test_sustained_contention_on_a_client_row_fails_it(sweep_fakes, monkeypatch, client_store_fake):
    stale = _client_row(client_store_fake)
    _rotate_to_b(monkeypatch)
    _rotate_back = TEST_KEK_B64

    async def _always_lose(client_row_id, new_blob, *, expected_blob):
        # A peer keeps writing a blob still under the previous key.
        monkeypatch.setenv("CONNECTORS_KEK", _rotate_back)
        reset_all_settings()
        client_store_fake.rows[0] = replace(
            stale, encrypted_blob=seal_credentials(stale.client_row_id, "s", "client_secret_post")
        )
        _rotate_to_b(monkeypatch)
        return False

    monkeypatch.setattr(client_store_fake, "rewrite_blob", _always_lose)
    result = await reencrypt_connection_tokens()
    assert result["failed_client_row_ids"] == [stale.client_row_id]
    assert result["cas_retries"] == reencrypt._MAX_CAS_RETRIES + 1


async def test_a_missing_kek_on_a_client_row_propagates(sweep_fakes, monkeypatch, client_store_fake):
    _client_row(client_store_fake)
    monkeypatch.delenv("CONNECTORS_KEK")
    reset_all_settings()
    with pytest.raises(crypto.ConnectorEncryptionConfigError):
        await reencrypt_connection_tokens()
