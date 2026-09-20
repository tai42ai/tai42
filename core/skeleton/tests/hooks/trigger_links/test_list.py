"""Listing trigger links: the multipage no-token prefix scan, orphan and
missing-field skips with their warnings, and the nil-name TTL race that logs
nothing."""

from __future__ import annotations

import json

from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.hooks.trigger_links import create_trigger_link, list_trigger_links


async def test_list_returns_records_and_prefix_no_token_multipage(store) -> None:
    tokens = {}
    for i in range(25):
        r = await create_trigger_link(
            topic="t",
            name=f"n{i:02d}",
            ttl_seconds=None,
            tool_kwargs={"i": i},
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by="bob",
        )
        tokens[r["name"]] = r["token"]
    listing = await list_trigger_links()
    assert listing["total"] == 25
    for record in listing["items"]:
        assert "token" not in record
        assert set(record) >= {
            "name",
            "topic",
            "execution_key",
            "tool_kwargs",
            "created_by",
            "created_at",
            "expires_at",
            "token_hash_prefix",
        }
        assert len(record["token_hash_prefix"]) == 12
        assert record["token_hash_prefix"] == hash_api_key(tokens[record["name"]])[:12]


async def test_list_skips_permanent_orphan_warns_but_nil_race_silent(store, caplog) -> None:
    # A live link.
    await create_trigger_link(
        topic="t",
        name="alive",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    # A PERMANENT orphan: a name key with a hash whose record is absent.
    store.redis._set_str(store.settings.trigger_name_key("orphan"), "a" * 64)
    with caplog.at_level("WARNING"):
        listing = await list_trigger_links()
    assert {r["name"] for r in listing["items"]} == {"alive"}
    assert "orphan" in caplog.text  # the permanent orphan is logged by name


async def test_list_skips_a_record_missing_a_required_field(store, caplog) -> None:
    # A live link.
    await create_trigger_link(
        topic="t",
        name="alive",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    # A corrupt record (body missing ``require_api_key``) is skipped with a WARNING
    # rather than taking the whole listing down.
    corrupt_hash = "d" * 64
    store.redis._set_str(store.settings.trigger_name_key("corrupt"), corrupt_hash)
    store.redis._set_str(
        store.settings.trigger_record_key(corrupt_hash),
        json.dumps(
            {
                "name": "corrupt",
                "topic": "t",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp-fire",
                "created_at": "2030-01-01T00:00:00",
            }
        ),
    )
    with caplog.at_level("WARNING"):
        listing = await list_trigger_links()
    assert {r["name"] for r in listing["items"]} == {"alive"}
    assert "corrupt" in caplog.text  # the corrupt record is logged by name
    assert "require_api_key" in caplog.text  # naming the missing field


async def test_list_nil_name_race_logs_nothing(store, caplog) -> None:
    await create_trigger_link(
        topic="t",
        name="alive",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    # An expiring name key that vanishes before MGET is a pure TTL race — no warning.
    store.redis._set_str(store.settings.trigger_name_key("racing"), "b" * 64, ex=5)
    store.redis.advance(6)
    with caplog.at_level("WARNING"):
        listing = await list_trigger_links()
    assert {r["name"] for r in listing["items"]} == {"alive"}
    assert "racing" not in caplog.text
