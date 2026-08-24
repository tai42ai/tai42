"""The Slack correlation stores: the thread (corr:<ts>) and form (form:<id>) contract
ports, plus the deliver-path front doors and the Events dedupe claim."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.channels import ChannelDeliveryError, Correlation

from tai42_channel_slack.correlation import (
    DEDUPE_TTL_SECONDS,
    claim_dedupe,
    delete_correlation,
    delete_form_record,
    get_form_record,
    release_dedupe,
    slack_form_correlation_store,
    slack_thread_correlation_store,
    store_correlation,
    store_form_record,
)

pytestmark = pytest.mark.usefixtures("slack_env")

_CALLBACK = "http://gateway/api/interactions/callback/ticket-9"
_SCHEMA = {"type": "object", "properties": {"full_name": {"type": "string"}}, "required": ["full_name"]}


def _deadline(seconds: float = 300) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _corr(callback_url: str = _CALLBACK, interaction_id: str = "int-1") -> Correlation:
    return Correlation(callback_url=callback_url, interaction_id=interaction_id, ttl_deadline=_deadline())


# -- the thread store (corr:<ts>) as the contract port ---------------------------


async def test_thread_set_get_round_trip(fake_redis):
    stored = await slack_thread_correlation_store.set_correlation(
        "11.22", _corr(interaction_id="int-9"), ttl_seconds=60
    )
    assert stored is True
    got = await slack_thread_correlation_store.get_correlation("11.22")
    assert got is not None
    assert got.callback_url == _CALLBACK
    assert got.interaction_id == "int-9"
    # Non-destructive peek: the reservation survives.
    assert await slack_thread_correlation_store.get_correlation("11.22") is not None


async def test_thread_get_unknown_returns_none(fake_redis):
    assert await slack_thread_correlation_store.get_correlation("99.99") is None


async def test_thread_set_is_nx(fake_redis):
    first = await slack_thread_correlation_store.set_correlation("11.22", _corr(interaction_id="a"), ttl_seconds=60)
    assert first is True
    second = await slack_thread_correlation_store.set_correlation("11.22", _corr(interaction_id="b"), ttl_seconds=60)
    assert second is False
    held = await slack_thread_correlation_store.get_correlation("11.22")
    assert held is not None
    assert held.interaction_id == "a"


async def test_thread_release_is_idempotent(fake_redis):
    await slack_thread_correlation_store.set_correlation("11.22", _corr(), ttl_seconds=60)
    await slack_thread_correlation_store.release_correlation("11.22")
    assert await slack_thread_correlation_store.get_correlation("11.22") is None
    await slack_thread_correlation_store.release_correlation("11.22")  # no-op, never an error


# -- the deliver-path front door for the thread store ----------------------------


async def test_store_correlation_writes_record_and_positive_ttl(fake_redis):
    timeout_at = datetime.now(UTC) + timedelta(seconds=120)

    await store_correlation("11.22", _CALLBACK, "int-9", timeout_at)

    record = json.loads(fake_redis.store["channel:slack:corr:11.22"])
    assert record["callback_url"] == _CALLBACK
    assert record["interaction_id"] == "int-9"  # interaction id now stored (additive)
    ttl = fake_redis.ttls["channel:slack:corr:11.22"]
    assert ttl is not None
    assert 0 < ttl <= 120


async def test_store_correlation_expired_budget_raises_and_writes_nothing(fake_redis):
    with pytest.raises(ChannelDeliveryError, match="budget already expired"):
        await store_correlation("11.22", _CALLBACK, "int-9", datetime.now(UTC) - timedelta(seconds=1))

    assert fake_redis.store == {}


async def test_delete_correlation_removes_mapping(fake_redis):
    await store_correlation("11.22", _CALLBACK, "int-9", datetime.now(UTC) + timedelta(seconds=60))
    await delete_correlation("11.22")
    assert await slack_thread_correlation_store.get_correlation("11.22") is None


# -- the form store (form:<interaction_id>) as the contract port -----------------


async def test_form_port_get_projects_the_rich_record(fake_redis):
    # The port get projects the SAME record store_form_record wrote down to the three
    # port fields (the interaction id IS the key), so the ladder can forward it.
    await store_form_record("int-9", _CALLBACK, _SCHEMA, "Give details", _deadline())
    got = await slack_form_correlation_store.get_correlation("int-9")
    assert got is not None
    assert got.callback_url == _CALLBACK
    assert got.interaction_id == "int-9"


async def test_form_port_get_unknown_returns_none(fake_redis):
    assert await slack_form_correlation_store.get_correlation("nope") is None


async def test_form_port_release_deletes_the_record(fake_redis):
    await store_form_record("int-9", _CALLBACK, _SCHEMA, "q", _deadline())
    await slack_form_correlation_store.release_correlation("int-9")
    assert await get_form_record("int-9") is None


async def test_store_form_record_writes_rich_json_and_positive_ttl(fake_redis):
    timeout_at = datetime.now(UTC) + timedelta(seconds=120)

    await store_form_record("int-9", _CALLBACK, _SCHEMA, "Give details", timeout_at)

    key = "channel:slack:form:int-9"
    record = json.loads(fake_redis.store[key])
    assert record == {
        "callback_url": _CALLBACK,
        "schema": _SCHEMA,
        "question": "Give details",
        "timeout_at": timeout_at.isoformat(),
    }
    ttl = fake_redis.ttls[key]
    assert ttl is not None
    assert 0 < ttl <= 120


async def test_store_form_record_expired_budget_raises_and_writes_nothing(fake_redis):
    with pytest.raises(ChannelDeliveryError, match="budget already expired"):
        await store_form_record("int-9", _CALLBACK, _SCHEMA, "q", datetime.now(UTC) - timedelta(seconds=1))

    assert fake_redis.store == {}


async def test_get_form_record_round_trips_and_misses(fake_redis):
    await store_form_record("int-9", _CALLBACK, _SCHEMA, "q", datetime.now(UTC) + timedelta(seconds=60))

    record = await get_form_record("int-9")
    assert record is not None
    assert record["schema"] == _SCHEMA
    assert await get_form_record("nope") is None


async def test_delete_form_record_removes_it(fake_redis):
    await store_form_record("int-9", _CALLBACK, _SCHEMA, "q", datetime.now(UTC) + timedelta(seconds=60))

    await delete_form_record("int-9")

    assert await get_form_record("int-9") is None


# -- the Events-API dedupe claim -------------------------------------------------


async def test_claim_dedupe_first_wins_second_loses(fake_redis):
    assert await claim_dedupe("Ev1") is True
    assert await claim_dedupe("Ev1") is False
    assert fake_redis.ttls["channel:slack:event:Ev1"] == DEDUPE_TTL_SECONDS


async def test_release_dedupe_allows_reclaim(fake_redis):
    assert await claim_dedupe("Ev1") is True
    await release_dedupe("Ev1")
    assert await claim_dedupe("Ev1") is True


async def test_missing_redis_url_raises_on_every_store_function(monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_SLACK_REDIS_URL")
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()

    for call in (
        slack_thread_correlation_store.set_correlation("11.22", _corr(), ttl_seconds=60),
        slack_thread_correlation_store.get_correlation("11.22"),
        slack_thread_correlation_store.release_correlation("11.22"),
        slack_form_correlation_store.get_correlation("int-9"),
        slack_form_correlation_store.release_correlation("int-9"),
        store_correlation("11.22", _CALLBACK, "int-9", _deadline()),
        delete_correlation("11.22"),
        store_form_record("int-9", _CALLBACK, _SCHEMA, "q", _deadline()),
        get_form_record("int-9"),
        delete_form_record("int-9"),
        claim_dedupe("Ev1"),
        release_dedupe("Ev1"),
    ):
        with pytest.raises(ValueError, match="CHANNEL_SLACK_REDIS_URL"):
            await call


async def test_specific_redis_url_configures_the_store(fake_redis):
    # The store URL is set by slack_env — a store call goes through, no config error.
    assert await slack_thread_correlation_store.get_correlation("nope") is None


async def test_default_namespace_redis_url_configures_the_store(fake_redis, monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_SLACK_REDIS_URL")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")
    reset_all_settings()

    assert await slack_thread_correlation_store.get_correlation("nope") is None
