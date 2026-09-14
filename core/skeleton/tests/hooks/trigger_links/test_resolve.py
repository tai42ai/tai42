"""Resolving a trigger token: 404 after revoke / ttl-expiry / late-bound verifier,
corrupt stored record raise, the byte-equal miss causes, the one-eval-per-op
structural pin, and a resolve-side erroring verifier."""

from __future__ import annotations

import json

import pytest
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.authz.execution import ExecutionKeyScan
from tai42_skeleton.hooks.trigger_links import (
    TriggerLinkError,
    create_trigger_link,
    resolve_trigger_token,
    restore_trigger_link,
    revoke_trigger_link,
)


async def test_resolve_on_late_bound_verifier_404_and_logs(store, caplog) -> None:
    result = await create_trigger_link(
        topic="late",
        name="l",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    await store.manager.set_topic_verifier("late", {"verifier": "hmac", "config": {}})
    with caplog.at_level("INFO"), pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token(result["token"])
    assert ei.value.status == 404
    assert "verifier-bound" in caplog.text


async def test_resolve_after_revoke_404(store) -> None:
    result = await create_trigger_link(
        topic="t",
        name="r",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    await revoke_trigger_link("r")
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token(result["token"])
    assert ei.value.status == 404


async def test_resolve_after_ttl_expiry_404_and_same_name_recreatable(store) -> None:
    result = await create_trigger_link(
        topic="t",
        name="timed",
        ttl_seconds=100,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    store.redis.advance(101)
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token(result["token"])
    assert ei.value.status == 404
    # The name key expired WITH the record (both-keys-EX), so the name is free again.
    again = await create_trigger_link(
        topic="t",
        name="timed",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    assert (await resolve_trigger_token(again["token"])).topic == "t"


async def test_five_miss_causes_are_byte_equal(store, monkeypatch, in_memory_store_factory) -> None:
    bodies: list[str] = []

    # unknown
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token("trg-never-existed")
    bodies.append(ei.value.message)

    # expired
    exp = await create_trigger_link(
        topic="t",
        name="m-exp",
        ttl_seconds=50,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    store.redis.advance(51)
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token(exp["token"])
    bodies.append(ei.value.message)

    # revoked
    rev = await create_trigger_link(
        topic="t",
        name="m-rev",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    await revoke_trigger_link("m-rev")
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token(rev["token"])
    bodies.append(ei.value.message)

    # verifier-bound
    vb = await create_trigger_link(
        topic="vb",
        name="m-vb",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    await store.manager.set_topic_verifier("vb", {"verifier": "h", "config": {}})
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token(vb["token"])
    bodies.append(ei.value.message)

    # in-memory
    in_memory_store_factory()
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token("trg-anything")
    bodies.append(ei.value.message)

    assert len(set(bodies)) == 1


async def test_erroring_verifier_at_resolve_propagates(store, monkeypatch) -> None:
    result = await create_trigger_link(
        topic="t",
        name="x",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )

    async def _boom(topic):
        raise RuntimeError("verifier store down")

    monkeypatch.setattr(store.manager, "get_topic_verifier", _boom)
    with pytest.raises(RuntimeError):
        await resolve_trigger_token(result["token"])


async def test_corrupt_stored_record_at_resolve_raises(store) -> None:
    token = "trg-corrupt-token"
    token_hash = hash_api_key(token)
    store.redis._set_str(store.settings.trigger_record_key(token_hash), "{not json")
    with pytest.raises(json.JSONDecodeError):
        await resolve_trigger_token(token)


async def test_create_revoke_restore_are_each_one_eval(store, monkeypatch) -> None:
    calls: list[str] = []
    original = store.redis.eval

    async def _counting(script, numkeys, *args):
        calls.append(script)
        return await original(script, numkeys, *args)

    monkeypatch.setattr(store.redis, "eval", _counting)
    await create_trigger_link(
        topic="t",
        name="one",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    assert sum("trigger:create:atomic" in c for c in calls) == 1
    await revoke_trigger_link("one")
    assert sum("trigger:revoke:atomic" in c for c in calls) == 1
    await restore_trigger_link(
        name="two",
        token_hash="a" * 64,
        record={
            "name": "two",
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
    assert sum("trigger:restore:atomic" in c for c in calls) == 1
