"""The route entry gate — the explicit flag, hashed multi-use codes (mint/get/list/
revoke), the glob-escaped SCAN, and the per-bucket guess throttle."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from tai42_channel_web.store.entry_gate import (
    EntryCode,
    check_entry_code,
    entry_attempt_allowed,
    get_entry_code,
    is_gate_enabled,
    list_entry_codes,
    mint_entry_code,
    revoke_entry_code,
    set_gate,
)

from .conftest import IDENTITY, OTHER_IDENTITY, FakeRedis

pytestmark = pytest.mark.usefixtures("web_env")

_GATE_KEY = f"channel:web:entry_gate:{IDENTITY}"


async def test_gate_flag_is_explicit(fake_redis: FakeRedis):
    # An ungated route reads False; the flag is set explicitly and read back True.
    assert await is_gate_enabled(IDENTITY) is False
    await set_gate(IDENTITY, True)
    assert fake_redis.store[_GATE_KEY] == "1"
    assert await is_gate_enabled(IDENTITY) is True
    await set_gate(IDENTITY, False)
    assert _GATE_KEY not in fake_redis.store
    assert await is_gate_enabled(IDENTITY) is False


async def test_mint_stores_only_the_hash_and_returns_the_raw_code_once(fake_redis: FakeRedis):
    raw_code, code_id = await mint_entry_code(IDENTITY, "spring launch", None)
    assert code_id == hashlib.sha256(raw_code.encode()).hexdigest()
    key = f"channel:web:entry_code:{IDENTITY}:{code_id}"
    # The raw code is never at rest — only its hash keys the record.
    assert raw_code not in json.dumps(fake_redis.store)
    stored = json.loads(fake_redis.store[key])
    assert stored["label"] == "spring launch"
    assert stored["expires_at"] is None
    # No expiry -> no TTL.
    assert key not in fake_redis.ttls


async def test_mint_with_an_expiry_sets_a_ttl(fake_redis: FakeRedis):
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    _, code_id = await mint_entry_code(IDENTITY, None, expires_at)
    key = f"channel:web:entry_code:{IDENTITY}:{code_id}"
    assert json.loads(fake_redis.store[key])["expires_at"] == expires_at.isoformat()
    assert 3500 <= fake_redis.ttls[key] <= 3600


async def test_mint_refuses_a_past_expiry(fake_redis: FakeRedis):
    with pytest.raises(ValueError, match="not in the future"):
        await mint_entry_code(IDENTITY, None, datetime.now(UTC) - timedelta(seconds=1))


async def test_check_entry_code_is_liveness_by_hash(fake_redis: FakeRedis):
    raw_code, _ = await mint_entry_code(IDENTITY, None, None)
    assert await check_entry_code(IDENTITY, raw_code) is True
    assert await check_entry_code(IDENTITY, "not-the-code") is False
    # A live code is scoped to its own route — the same raw value is unknown elsewhere.
    assert await check_entry_code(OTHER_IDENTITY, raw_code) is False


async def test_get_and_list_expose_metadata_never_the_raw_code(fake_redis: FakeRedis):
    raw_code, code_id = await mint_entry_code(IDENTITY, "launch", None)
    fetched = await get_entry_code(IDENTITY, code_id)
    assert fetched is not None
    assert fetched == EntryCode(code_id=code_id, label="launch", created_at=fetched.created_at, expires_at=None)
    listed = await list_entry_codes(IDENTITY)
    assert [code.code_id for code in listed] == [code_id]
    assert raw_code not in json.dumps([code.__dict__ for code in listed])


async def test_revoke_kills_the_code_and_reports_whether_it_existed(fake_redis: FakeRedis):
    raw_code, code_id = await mint_entry_code(IDENTITY, None, None)
    assert await revoke_entry_code(IDENTITY, code_id) is True
    assert await check_entry_code(IDENTITY, raw_code) is False
    # A second revoke of the same id finds nothing.
    assert await revoke_entry_code(IDENTITY, code_id) is False


async def test_deleting_every_code_leaves_the_gate_closed(fake_redis: FakeRedis):
    # The gate flag is EXPLICIT: revoking the last code does not silently reopen the
    # route — it stays gated, and now unreachable until a code is minted.
    await set_gate(IDENTITY, True)
    _, code_id = await mint_entry_code(IDENTITY, None, None)
    await revoke_entry_code(IDENTITY, code_id)
    assert await is_gate_enabled(IDENTITY) is True
    assert await list_entry_codes(IDENTITY) == []


async def test_list_escapes_a_glob_metachar_identity(fake_redis: FakeRedis):
    # ``_clean_identity`` admits Redis glob metacharacters; an unescaped ``*`` in the
    # SCAN pattern would match every sibling identity's keys. The escape keeps the
    # segment literal, so a ``*`` identity matches only its OWN codes.
    _, star_code = await mint_entry_code("*", None, None)
    await mint_entry_code("site-alpha", None, None)
    await mint_entry_code("site-beta", None, None)
    listed = await list_entry_codes("*")
    assert [code.code_id for code in listed] == [star_code]


async def test_throttle_allows_up_to_the_cap_then_refuses(fake_redis: FakeRedis):
    bucket = "net-1.2.3.4"
    key = f"channel:web:entry_throttle:{bucket}"
    # The default cap is 10 per window.
    allowed = [await entry_attempt_allowed(bucket) for _ in range(11)]
    assert allowed == [True] * 10 + [False]
    # The window is set once, on the first attempt.
    assert fake_redis.ttls[key] == 300


async def test_throttle_is_per_bucket(fake_redis: FakeRedis):
    for _ in range(10):
        await entry_attempt_allowed("net-a")
    assert await entry_attempt_allowed("net-a") is False
    # A different bucket has its own budget.
    assert await entry_attempt_allowed("net-b") is True
