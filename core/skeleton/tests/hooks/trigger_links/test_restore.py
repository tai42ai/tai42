"""Restore and restore_tombstone: the validation refusals, displacement vs
tombstone semantics, expiry handling, sub-second EX ceiling, idempotency, and
cross-name tombstone isolation."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.authz.execution import ExecutionKeyScan
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

from ._records import name_keys, rec_keys, tomb_keys, valid_record


@pytest.mark.parametrize(
    ("name", "token_hash", "match"),
    [
        (123, "a" * 64, "name must be a string"),
        ("ok", 456, "token_hash must be a string"),
    ],
)
async def test_restore_non_string_name_or_hash_400(store, name, token_hash, match) -> None:
    with pytest.raises(TriggerLinkError) as ei:
        await restore_trigger_link(
            name=name,
            token_hash=token_hash,
            record={
                "name": "ok",
                "topic": "t",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp-fire",
                "require_api_key": False,
                "tool_kwargs": None,
                "created_by": None,
                "created_at": "2030-01-01T00:00:00",
                "expires_at": None,
            },
            scan=ExecutionKeyScan(),
        )
    assert ei.value.status == 400
    assert match in ei.value.message
    assert name_keys(store) == []  # nothing written on a rejected triple


async def test_restore_stores_the_validated_body_not_the_imported_one(store) -> None:
    # Restore stores what the record MODEL accepted, so a hand-edited ``"false"`` is
    # enforced as False, never re-coerced from the truthy raw string at read.
    await restore_trigger_link(
        name="coerced",
        token_hash="b" * 64,
        record={
            "name": "coerced",
            "topic": "t",
            "execution_key": "k-fire",
            "execution_key_fingerprint": "fp-fire",
            "require_api_key": "false",
            "tool_kwargs": None,
            "created_by": None,
            "created_at": "2030-01-01T00:00:00",
            "expires_at": None,
        },
        scan=ExecutionKeyScan(),
    )
    (record,) = [r for r in (await list_trigger_links())["items"] if r["name"] == "coerced"]
    assert record["require_api_key"] is False
    assert record["trigger_auth"] == "token"


async def test_restore_refuses_name_mismatch(store) -> None:
    with pytest.raises(TriggerLinkError):
        await restore_trigger_link(
            name="idx", token_hash="a" * 64, record=valid_record(name="other"), scan=ExecutionKeyScan()
        )


@pytest.mark.parametrize("execution_key", [None, ""])
async def test_restore_refuses_a_record_with_no_execution_key(store, execution_key) -> None:
    # A record missing or emptying its execution key is refused outright: never revived,
    # never defaulted to a privileged principal.
    record = valid_record()
    if execution_key is None:
        del record["execution_key"]
    else:
        record["execution_key"] = execution_key
    with pytest.raises(TriggerLinkError) as ei:
        await restore_trigger_link(name="rn", token_hash="a" * 64, record=record, scan=ExecutionKeyScan())
    assert ei.value.status == 400
    assert "execution_key" in ei.value.message
    assert name_keys(store) == []


@pytest.mark.parametrize("bad_name", ["foo/bar", "-lead", "", "a" * 65])
async def test_restore_refuses_pattern_violating_name(store, bad_name) -> None:
    with pytest.raises(TriggerLinkError):
        await restore_trigger_link(
            name=bad_name, token_hash="a" * 64, record=valid_record(name=bad_name), scan=ExecutionKeyScan()
        )


@pytest.mark.parametrize("bad_hash", ["nothex", "A" * 64, "a" * 63, "a" * 65])
async def test_restore_refuses_non_hex_hash(store, bad_hash) -> None:
    with pytest.raises(TriggerLinkError):
        await restore_trigger_link(name="rn", token_hash=bad_hash, record=valid_record(), scan=ExecutionKeyScan())


async def test_restore_refuses_unparseable_expires_at(store) -> None:
    record = valid_record()
    record["expires_at"] = "not-a-timestamp"
    with pytest.raises(TriggerLinkError):
        await restore_trigger_link(name="rn", token_hash="a" * 64, record=record, scan=ExecutionKeyScan())


async def test_restore_refuses_offset_less_expires_at(store) -> None:
    # A naive deadline must be a TYPED refusal: the bare TypeError from comparing it to
    # "now" escapes the per-record guard and tears the section mid-write.
    record = valid_record()
    record["expires_at"] = "2030-01-01T00:00:00"
    with pytest.raises(TriggerLinkError, match="carries no timezone offset"):
        await restore_trigger_link(name="rn", token_hash="a" * 64, record=record, scan=ExecutionKeyScan())


@pytest.mark.parametrize("mutate", [{"topic": ""}, {"tool_kwargs": [1, 2]}])
async def test_restore_refuses_malformed_body(store, mutate) -> None:
    record = valid_record()
    record.update(mutate)
    with pytest.raises(TriggerLinkError):
        await restore_trigger_link(name="rn", token_hash="a" * 64, record=record, scan=ExecutionKeyScan())


async def test_restore_refuses_hash_with_trailing_newline(store) -> None:
    with pytest.raises(TriggerLinkError) as ei:
        await restore_trigger_link(
            name="rn", token_hash="a" * 64 + "\n", record=valid_record(name="rn"), scan=ExecutionKeyScan()
        )
    assert ei.value.status == 400


async def test_restore_refuses_unknown_field(store) -> None:
    record = valid_record(name="rn")
    record["unexpected_field"] = "x"
    with pytest.raises(TriggerLinkError) as ei:
        await restore_trigger_link(name="rn", token_hash="a" * 64, record=record, scan=ExecutionKeyScan())
    assert ei.value.status == 400


async def test_restore_refuses_hash_live_under_different_name(store) -> None:
    result = await create_trigger_link(
        topic="t",
        name="livename",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    token_hash = hash_api_key(result["token"])
    with pytest.raises(TriggerLinkError) as ei:
        await restore_trigger_link(
            name="othername", token_hash=token_hash, record=valid_record(name="othername"), scan=ExecutionKeyScan()
        )
    assert ei.value.status == 400


async def test_restore_after_revoke_refused_tombstoned(store) -> None:
    result = await create_trigger_link(
        topic="t",
        name="tw",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    exported = (await export_trigger_links())["trigger_links"][0]
    await revoke_trigger_link("tw")
    outcome = await restore_trigger_link(
        name=exported["name"], token_hash=exported["token_hash"], record=exported["record"], scan=ExecutionKeyScan()
    )
    assert outcome == "skipped_tombstoned"
    # No live pair slipped in behind the tombstone.
    with pytest.raises(TriggerLinkError):
        await resolve_trigger_token(result["token"])


async def test_restore_over_live_different_hash_deletes_displaced_no_tombstone(store) -> None:
    first = await create_trigger_link(
        topic="t",
        name="shared",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    first_hash = hash_api_key(first["token"])
    # A second exported record under the SAME name but a different hash.
    other_hash = "c" * 64
    outcome = await restore_trigger_link(
        name="shared", token_hash=other_hash, record=valid_record(name="shared"), scan=ExecutionKeyScan()
    )
    assert outcome == "updated"
    # The displaced hash's record key is gone, and NO tombstone was written for it.
    assert store.settings.trigger_record_key(first_hash) not in store.redis._strings
    assert tomb_keys(store) == []
    with pytest.raises(TriggerLinkError):
        await resolve_trigger_token(first["token"])


async def test_restore_over_same_hash_updates_still_resolves_no_tombstone(store) -> None:
    result = await create_trigger_link(
        topic="t",
        name="same",
        ttl_seconds=None,
        tool_kwargs={"k": 1},
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    exported = (await export_trigger_links())["trigger_links"][0]
    outcome = await restore_trigger_link(
        name=exported["name"], token_hash=exported["token_hash"], record=exported["record"], scan=ExecutionKeyScan()
    )
    assert outcome == "updated"
    assert tomb_keys(store) == []
    resolved = await resolve_trigger_token(result["token"])
    assert (resolved.topic, resolved.tool_kwargs) == ("t", {"k": 1})


async def test_restore_expired_record_skipped(store) -> None:
    record = valid_record()
    record["expires_at"] = "2000-01-01T00:00:00+00:00"
    outcome = await restore_trigger_link(name="rn", token_hash="a" * 64, record=record, scan=ExecutionKeyScan())
    assert outcome == "skipped_expired"
    assert rec_keys(store) == []


async def test_restore_sub_second_remaining_lives_with_ex_one(store, monkeypatch) -> None:
    # A record whose deadline is 0.4s away restores with EX 1 (ceil), never EX 0.
    from tai42_skeleton.hooks import trigger_links as tl

    deadline = "2026-07-21T00:00:00.400000+00:00"
    monkeypatch.setattr(
        tl,
        "datetime",
        SimpleNamespace(
            fromisoformat=datetime.fromisoformat,
            now=lambda tz=None: datetime.fromisoformat("2026-07-21T00:00:00+00:00"),
        ),
    )
    record = valid_record()
    record["expires_at"] = deadline
    outcome = await restore_trigger_link(name="rn", token_hash="d" * 64, record=record, scan=ExecutionKeyScan())
    assert outcome in ("created", "updated")
    # The record lives (EX 1, not EX 0 which errors); the key is present at t=0.
    assert store.settings.trigger_record_key("d" * 64) in store.redis._strings


async def test_restore_tombstone_idempotent(store) -> None:
    h = "e" * 64
    await restore_tombstone(h)
    await restore_tombstone(h)
    assert store.settings.trigger_tomb_key(h) in store.redis._strings


async def test_restore_tombstone_kills_a_link_still_live_under_that_hash(store) -> None:
    # An imported tombstone removes the local record AND name key, or a link whose door
    # 404s stays in the management surface and in every later export.
    minted = await create_trigger_link(
        topic="t",
        name="zombie",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    token_hash = hash_api_key(minted["token"])

    await restore_tombstone(token_hash)

    assert store.settings.trigger_tomb_key(token_hash) in store.redis._strings
    assert store.settings.trigger_record_key(token_hash) not in store.redis._strings
    assert store.settings.trigger_name_key("zombie") not in store.redis._strings
    assert (await list_trigger_links())["total"] == 0
    assert (await export_trigger_links())["trigger_links"] == []
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token(minted["token"])
    assert ei.value.status == 404


async def test_restore_tombstone_leaves_a_name_rebound_to_another_link_alone(store) -> None:
    # The name index is only this tombstone's to remove while it still points AT this
    # hash; a name since re-bound to a different link belongs to that link.
    first = await create_trigger_link(
        topic="t",
        name="reused",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    first_hash = hash_api_key(first["token"])
    await revoke_trigger_link("reused")
    second = await create_trigger_link(
        topic="t",
        name="reused",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )

    await restore_tombstone(first_hash)

    assert store.settings.trigger_name_key("reused") in store.redis._strings
    assert (await resolve_trigger_token(second["token"])).topic == "t"


@pytest.mark.parametrize("token_hash", [123])
async def test_restore_tombstone_non_string_hash_400(store, token_hash) -> None:
    # A non-str token_hash is a typed refusal, never a raw TypeError out of the regex.
    with pytest.raises(TriggerLinkError, match="token_hash must be a string"):
        await restore_tombstone(token_hash)
