"""The api-key door requirement across create/list/resolve/restore: the round-trip
and its derived listing axis, reporting only where the gate enforces it, restore
refusal without it, out-of-service reporting under a verifier binding, and the loud
resolve of a record missing it."""

from __future__ import annotations

import json

import pytest
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.authz.execution import ExecutionKeyScan
from tai42_skeleton.hooks.trigger_links import (
    TriggerLinkError,
    create_trigger_link,
    list_trigger_links,
    resolve_trigger_token,
    restore_trigger_link,
)

from ._records import name_keys


async def test_the_door_requirement_round_trips_and_rides_the_listing(store) -> None:
    # The link door's auth axis: ``require_api_key`` is the ONE piece of stored state, and
    # the axis value the listing reports is DERIVED from it at every read.
    token_only = await create_trigger_link(
        topic="t",
        name="open",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    authed = await create_trigger_link(
        topic="t",
        name="authed",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=True,
        created_by=None,
    )

    assert (await resolve_trigger_token(token_only["token"])).require_api_key is False
    assert (await resolve_trigger_token(authed["token"])).require_api_key is True

    by_name = {record["name"]: record for record in (await list_trigger_links())["items"]}
    assert by_name["open"]["trigger_auth"] == "token"
    assert by_name["authed"]["trigger_auth"] == "token+api_key"


async def test_the_door_requirement_is_reported_only_where_it_is_enforced(store, monkeypatch) -> None:
    # With the gate off the door's own check admits every caller, so reporting the
    # record's stored requirement would advertise an api-key gate that does not exist.
    from tai42_skeleton.access_control.settings import AccessControlSettings
    from tai42_skeleton.hooks import trigger_auth

    await create_trigger_link(
        topic="t",
        name="authed",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=True,
        created_by=None,
    )
    monkeypatch.setattr(trigger_auth, "access_control_settings", lambda: AccessControlSettings(enable=False))
    (record,) = (await list_trigger_links())["items"]
    assert record["require_api_key"] is True
    assert record["trigger_auth"] == "token"


async def test_restore_refuses_a_record_without_the_door_requirement(store) -> None:
    # A body missing ``require_api_key`` is corruption, refused per-record: a permissive
    # default would serve a link minted ``token+api_key`` as token-only.
    with pytest.raises(TriggerLinkError) as ei:
        await restore_trigger_link(
            name="flagless",
            token_hash="a" * 64,
            record={
                "name": "flagless",
                "topic": "t",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp-fire",
                "tool_kwargs": None,
                "created_by": None,
                "created_at": "2030-01-01T00:00:00",
                "expires_at": None,
            },
            scan=ExecutionKeyScan(),
        )
    assert ei.value.status == 400
    assert "require_api_key" in ei.value.message
    assert name_keys(store) == []


async def test_a_verifier_bound_topic_reports_its_links_out_of_service(store) -> None:
    # A topic verifier binding takes the topic's links out of service without touching
    # a record, so the listing reports the door's live behavior, not the stored flag.
    minted = await create_trigger_link(
        topic="events",
        name="qr",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=True,
        created_by=None,
    )
    (before,) = (await list_trigger_links())["items"]
    assert before["trigger_auth"] == "token+api_key"

    await store.manager.set_topic_verifier("events", {"verifier": "github", "config": {}})

    (after,) = (await list_trigger_links())["items"]
    assert after["trigger_auth"] == "out-of-service"
    with pytest.raises(TriggerLinkError) as ei:
        await resolve_trigger_token(minted["token"])
    assert ei.value.status == 404


async def test_a_record_missing_the_door_requirement_is_a_loud_resolve(store) -> None:
    # The fire path reads a missing ``require_api_key`` loudly: a silent permissive
    # default would serve a link minted ``token+api_key`` as token-only.
    token = "trg-corrupt"
    token_hash = hash_api_key(token)
    store.redis._set_str(
        store.settings.trigger_record_key(token_hash),
        json.dumps(
            {
                "name": "corrupt",
                "topic": "t",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp-fire",
                "tool_kwargs": None,
                "created_by": None,
                "created_at": "2030-01-01T00:00:00",
                "expires_at": None,
            }
        ),
    )
    with pytest.raises(KeyError):
        await resolve_trigger_token(token)
