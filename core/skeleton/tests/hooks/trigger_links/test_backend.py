"""Backend-level checks: the extended ``FakeRedis`` string/clock/scan behaviours,
the ``_scan_all`` rehash dedupe, the in-memory backend's refusals and truthfully
empty export, and the create/resolve/revoke log doctrine (no raw token, correlation
by hash prefix)."""

from __future__ import annotations

import pytest

from tai42_skeleton.authz.execution import ExecutionKeyScan
from tai42_skeleton.hooks import trigger_links
from tai42_skeleton.hooks.trigger_links import (
    TriggerLinkError,
    create_trigger_link,
    export_trigger_links,
    list_trigger_links,
    resolve_trigger_token,
    restore_tombstone,
    restore_trigger_link,
    revoke_trigger_link,
)

from ._records import valid_record


async def test_fake_set_get_delete_exists(fake_redis) -> None:
    assert await fake_redis.set("k", "v") is True
    assert await fake_redis.get("k") == "v"
    assert await fake_redis.exists("k") == 1
    assert await fake_redis.delete("k") == 1
    assert await fake_redis.get("k") is None
    assert await fake_redis.exists("k") == 0


async def test_fake_ex_expiry_uses_injectable_clock(fake_redis) -> None:
    await fake_redis.set("k", "v", ex=10)
    fake_redis.advance(9)
    assert await fake_redis.get("k") == "v"
    fake_redis.advance(1)
    assert await fake_redis.get("k") is None


async def test_fake_mget_and_paging_scan(fake_redis) -> None:
    for i in range(25):
        await fake_redis.set(f"p:{i:02d}", str(i))
    assert await fake_redis.mget(["p:00", "missing", "p:24"]) == ["0", None, "24"]
    seen: list[str] = []
    cursor = 0
    pages = 0
    while True:
        cursor, batch = await fake_redis.scan(cursor, match="p:*", count=100)
        seen.extend(batch)
        pages += 1
        if cursor == 0:
            break
    assert len(seen) == 25
    assert pages >= 3  # deliberately multi-page, so a first-page-only bug fails


async def test_scan_all_dedupes_rehash_duplicate_keys() -> None:
    class _DupScan:
        def __init__(self) -> None:
            self._calls = 0

        async def scan(self, cursor, match=None, count=None):
            self._calls += 1
            if self._calls == 1:
                return 1, ["k:a", "k:a", "k:b"]
            return 0, ["k:a"]

    result = await trigger_links._scan_all(_DupScan(), "k:*")
    assert result == ["k:a", "k:b"]


async def test_in_memory_crud_501_resolve_404(in_memory_store) -> None:
    for coro in (
        create_trigger_link(
            topic="t",
            name="x",
            ttl_seconds=None,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        ),
        list_trigger_links(),
        revoke_trigger_link("x"),
        restore_tombstone("a" * 64),
        restore_trigger_link(name="x", token_hash="a" * 64, record=valid_record(name="x"), scan=ExecutionKeyScan()),
    ):
        with pytest.raises(TriggerLinkError) as ei:
            await coro
        assert ei.value.status == 501
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token("trg-anything")
    assert ei.value.status == 404


async def test_in_memory_export_truthfully_empty(in_memory_store) -> None:
    assert await export_trigger_links() == {"trigger_links": [], "tombstones": []}


async def test_log_doctrine_no_raw_token_and_correlation(store, caplog) -> None:
    with caplog.at_level("INFO"):
        result = await create_trigger_link(
            topic="topicX",
            name="logname",
            ttl_seconds=1200,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by="carol",
        )
        listing = await list_trigger_links()
        await resolve_trigger_token(result["token"])
        await revoke_trigger_link("logname")
    text = caplog.text
    # create line carries caller + name + topic + ttl.
    for token_part in ("carol", "logname", "topicX", "1200"):
        assert token_part in text
    # resolve outcome line carries the hash prefix matching the list's token_hash_prefix.
    (record,) = [r for r in listing["items"] if r["name"] == "logname"]
    assert record["token_hash_prefix"] in text
    # revoke line carries name + hash prefix.
    assert "revoked name=logname" in text
    # NEVER the raw token.
    assert result["token"] not in text
