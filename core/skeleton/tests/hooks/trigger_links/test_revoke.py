"""Revoking a trigger link: the tombstone write and the uniform resolve-404 it
yields, the unknown-name 404, and the no-orphan revoke-vs-recreate orderings."""

from __future__ import annotations

import json

import pytest
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.hooks.trigger_links import (
    TriggerLinkError,
    create_trigger_link,
    resolve_trigger_token,
    revoke_trigger_link,
)

from ._records import name_keys, rec_keys


async def test_revoke_writes_tombstone_and_resolve_of_tombstoned_hash_404(store) -> None:
    result = await create_trigger_link(
        topic="t",
        name="rev",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    token_hash = hash_api_key(result["token"])
    await revoke_trigger_link("rev")
    assert store.settings.trigger_tomb_key(token_hash) in store.redis._strings
    # Belt-and-braces: even with a live record key present, a tombstone → uniform 404.
    store.redis._set_str(store.settings.trigger_record_key(token_hash), json.dumps({"topic": "t"}))
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token(result["token"])
    assert ei.value.status == 404
    with pytest.raises(TriggerLinkError) as unknown:
        await resolve_trigger_token("trg-unknown")
    assert ei.value.message == unknown.value.message


async def test_revoke_unknown_name_404(store) -> None:
    with pytest.raises(TriggerLinkError) as ei:
        await revoke_trigger_link("nope")
    assert ei.value.status == 404


async def test_no_orphan_revoke_vs_recreate_both_orderings(store) -> None:
    a = await create_trigger_link(
        topic="t",
        name="x",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    await revoke_trigger_link("x")
    b = await create_trigger_link(
        topic="t",
        name="x",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    # Exactly one live name key + one live record key for the surviving link.
    assert len(name_keys(store)) == 1
    assert len(rec_keys(store)) == 1
    assert (await resolve_trigger_token(b["token"])).topic == "t"
    # The revoked token is dead.
    with pytest.raises(TriggerLinkError):
        await resolve_trigger_token(a["token"])
