"""Creating a trigger link: timed and permanent roundtrips, tool-kwargs handling,
the mint-door 400s, ttl bounds, name validation, default-name generation and
collision retry, duplicate-name 409, and create-side fail-closed behaviours."""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from tai42_skeleton.hooks import trigger_links
from tai42_skeleton.hooks.trigger_links import (
    TriggerLinkError,
    create_trigger_link,
    list_trigger_links,
    resolve_trigger_token,
    revoke_trigger_link,
)

from ._records import name_keys, rec_keys


async def test_create_timed_roundtrip_with_tool_kwargs(store) -> None:
    result = await create_trigger_link(
        topic="events",
        name="link1",
        ttl_seconds=3600,
        tool_kwargs={"a": 1},
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by="alice",
    )
    assert result["name"] == "link1"
    assert result["topic"] == "events"
    assert result["trigger_path"] == f"/trigger/{result['token']}"
    assert result["expires_at"] is not None
    resolved = await resolve_trigger_token(result["token"])
    assert (resolved.topic, resolved.tool_kwargs, resolved.execution_key) == ("events", {"a": 1}, "k-fire")


async def test_create_permanent_roundtrip_without_tool_kwargs(store) -> None:
    result = await create_trigger_link(
        topic="events",
        name="perm",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    assert result["expires_at"] is None
    resolved = await resolve_trigger_token(result["token"])
    assert (resolved.topic, resolved.tool_kwargs) == ("events", None)


async def test_create_empty_tool_kwargs_stored_verbatim(store) -> None:
    result = await create_trigger_link(
        topic="t",
        name="e",
        ttl_seconds=None,
        tool_kwargs={},
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    assert (await resolve_trigger_token(result["token"])).tool_kwargs == {}


async def test_create_non_dict_tool_kwargs_400(store) -> None:
    with pytest.raises(TriggerLinkError) as ei:
        await create_trigger_link(
            topic="t",
            name="x",
            ttl_seconds=None,
            tool_kwargs=[1, 2],  # type: ignore[arg-type]
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
    assert ei.value.status == 400


async def test_create_empty_topic_400(store) -> None:
    # The stored record model refuses an empty topic, so the mint door must too — a
    # link minted on "" would be unrestorable from its own backup.
    with pytest.raises(TriggerLinkError) as ei:
        await create_trigger_link(
            topic="",
            name="x",
            ttl_seconds=None,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
    assert ei.value.status == 400
    assert "topic must be a non-empty string" in ei.value.message
    assert name_keys(store) == []


async def test_create_empty_execution_key_400(store) -> None:
    # Same rule for the identity field: the mint door is reached flat (projected MCP
    # tool, direct call) without the body model, so it must enforce ``min_length=1``
    # itself — a link minted with "" is dropped by its own backup's restore.
    with pytest.raises(TriggerLinkError) as ei:
        await create_trigger_link(
            topic="t",
            name="x",
            ttl_seconds=None,
            tool_kwargs=None,
            execution_key="",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
    assert ei.value.status == 400
    assert "execution_key must be a non-empty string" in ei.value.message
    assert name_keys(store) == []


async def test_expires_at_minus_created_at_equals_ttl(store) -> None:
    result = await create_trigger_link(
        topic="t",
        name="ttlpin",
        ttl_seconds=3600,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    listing = await list_trigger_links()
    (record,) = [r for r in listing["items"] if r["name"] == "ttlpin"]
    delta = datetime.fromisoformat(result["expires_at"]) - datetime.fromisoformat(record["created_at"])
    assert delta.total_seconds() == 3600


@pytest.mark.parametrize("bad_ttl", [0, -1, -3600])
async def test_ttl_zero_or_negative_400(store, bad_ttl) -> None:
    with pytest.raises(TriggerLinkError) as ei:
        await create_trigger_link(
            topic="t",
            name="x",
            ttl_seconds=bad_ttl,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
    assert ei.value.status == 400


async def test_physical_ttl_bound_boundary(store) -> None:
    ok = await create_trigger_link(
        topic="t",
        name="onbound",
        ttl_seconds=10**10,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    assert ok["name"] == "onbound"
    for over in (10**10 + 1, 10**16):
        with pytest.raises(TriggerLinkError) as ei:
            await create_trigger_link(
                topic="t",
                name=f"over{over}",
                ttl_seconds=over,
                tool_kwargs=None,
                execution_key="k-fire",
                execution_key_fingerprint="fp-fire",
                require_api_key=False,
                created_by=None,
            )
        assert ei.value.status == 400


_INVALID_NAMES = ["foo/bar", "a" * 65, "", ".", "..", "-", "--", ".-", "bad name", "bad$char", "a\tb", "abc\n"]


@pytest.mark.parametrize("name", _INVALID_NAMES)
async def test_invalid_name_400(store, name) -> None:
    with pytest.raises(TriggerLinkError) as ei:
        await create_trigger_link(
            topic="t",
            name=name,
            ttl_seconds=None,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
    assert ei.value.status == 400
    assert name_keys(store) == []  # nothing written on a rejected name


async def test_64_char_name_accepted(store) -> None:
    name = "a" * 64
    result = await create_trigger_link(
        topic="t",
        name=name,
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    assert result["name"] == name


async def test_nameless_create_uses_default_name(store) -> None:
    result = await create_trigger_link(
        topic="t",
        name=None,
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    assert re.fullmatch(r"trg-link-[0-9a-f]{8}", result["name"])
    listing = await list_trigger_links()
    assert result["name"] in {r["name"] for r in listing["items"]}
    await revoke_trigger_link(result["name"])  # round-trips through revoke


async def test_generated_name_first_collision_then_fresh_succeeds(store, monkeypatch) -> None:
    await create_trigger_link(
        topic="t",
        name="taken-gen",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    names = iter(["taken-gen", "fresh-gen"])
    monkeypatch.setattr(trigger_links, "_default_name", lambda: next(names))
    result = await create_trigger_link(
        topic="t",
        name=None,
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    assert result["name"] == "fresh-gen"


async def test_generated_name_double_collision_raises(store, monkeypatch) -> None:
    await create_trigger_link(
        topic="t",
        name="always",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    monkeypatch.setattr(trigger_links, "_default_name", lambda: "always")
    with pytest.raises(TriggerLinkError) as ei:
        await create_trigger_link(
            topic="t",
            name=None,
            ttl_seconds=None,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
    assert ei.value.status == 409


async def test_duplicate_explicit_name_409_loser_record_not_written(store) -> None:
    await create_trigger_link(
        topic="t",
        name="dup",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    before = set(rec_keys(store))
    with pytest.raises(TriggerLinkError) as ei:
        await create_trigger_link(
            topic="t",
            name="dup",
            ttl_seconds=None,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
    assert ei.value.status == 409
    # The loser minted a fresh token but its record key was never written.
    assert set(rec_keys(store)) == before


async def test_create_refused_on_verifier_bound_topic_400(store) -> None:
    await store.manager.set_topic_verifier("secure", {"verifier": "hmac", "config": {}})
    with pytest.raises(TriggerLinkError) as ei:
        await create_trigger_link(
            topic="secure",
            name="x",
            ttl_seconds=None,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
    assert ei.value.status == 400
    assert name_keys(store) == []


async def test_erroring_verifier_at_create_propagates_nothing_written(store, monkeypatch) -> None:
    async def _boom(topic):
        raise RuntimeError("verifier store down")

    monkeypatch.setattr(store.manager, "get_topic_verifier", _boom)
    with pytest.raises(RuntimeError):
        await create_trigger_link(
            topic="t",
            name="x",
            ttl_seconds=None,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
    assert name_keys(store) == []
    assert rec_keys(store) == []
