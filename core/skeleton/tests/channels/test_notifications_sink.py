"""The internal notifications sink: a record -> read roundtrip against the fake
pooled redis (newest-first, id + server-side timestamp minted per record), and
the module-level helpers that open the interactions connection. Every stored
record is self-contained JSON; a malformed one raises on read, never skipped.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from tai42_skeleton.channels import notifications_sink
from tai42_skeleton.channels.notifications_sink import NotificationSink
from tai42_skeleton.interactions.settings import InteractionsSettings

# The default interactions key prefix, the namespace the sink shares with the
# interactions keys; the direct-sink tests build a sink under it explicitly.
_DEFAULT_PREFIX = "interactions:"

# A feed bound comfortably above the handful of records the roundtrip tests write,
# so their assertions see every record; the retention test sets its own small cap.
_TEST_FEED_MAX = 1000

# The per-audience feed idle TTL (seconds) the direct-sink tests build a sink with;
# the TTL test drives the fake clock past it.
_TEST_FEED_TTL = 30 * 86400


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``record_notification`` refuses when the interactions store is OFF; these tests
    # exercise the ON sink, so configure its store — the fake connection still stands
    # in, only the presence gate reads this env var.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


async def test_record_returns_the_stored_record(fake_redis) -> None:
    sink = NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, _TEST_FEED_TTL)

    record = await sink.record(fake_redis, "deploy started", "ops")

    assert record["message"] == "deploy started"
    assert record["recipient"] == "ops"
    assert record["id"]
    assert record["created_at"]


async def test_read_returns_records_newest_first(fake_redis) -> None:
    sink = NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, _TEST_FEED_TTL)

    first = await sink.record(fake_redis, "first", None)
    second = await sink.record(fake_redis, "second", "user-9")

    records = await sink.read(fake_redis)

    assert records == [second, first]
    assert records[0]["recipient"] == "user-9"
    assert records[1]["recipient"] is None


async def test_read_empty_sink_returns_empty_list(fake_redis) -> None:
    assert await NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, _TEST_FEED_TTL).read(fake_redis) == []


async def test_record_evicts_oldest_beyond_the_feed_cap(fake_redis) -> None:
    # The feed is a bounded newest-first ring buffer: writing more than the cap
    # keeps the newest ``cap`` records and evicts the oldest by design (LTRIM), so
    # the feed never grows past the cap.
    cap = 3
    sink = NotificationSink(_DEFAULT_PREFIX, cap, _TEST_FEED_TTL)

    written = [await sink.record(fake_redis, f"msg-{i}", None) for i in range(cap + 2)]

    records = await sink.read(fake_redis)
    # Only the newest ``cap`` survive, newest-first; the two oldest were evicted.
    assert records == list(reversed(written[-cap:]))
    assert len(records) == cap
    assert len(fake_redis._lists[f"{_DEFAULT_PREFIX}notifications:feed"]) == cap
    evicted_ids = {written[0]["id"], written[1]["id"]}
    assert evicted_ids.isdisjoint(rec["id"] for rec in records)


async def test_read_raises_on_a_malformed_record(fake_redis) -> None:
    sink = NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, _TEST_FEED_TTL)
    # A non-JSON member is corrupt state, not an empty result: it must surface
    # loudly on read rather than being silently dropped.
    await fake_redis.lpush(f"{_DEFAULT_PREFIX}notifications:feed", "not-json")

    with pytest.raises(json.JSONDecodeError):
        await sink.read(fake_redis)


@pytest.fixture
def sink_redis(monkeypatch, fake_redis):
    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(notifications_sink, "client_ctx", _ctx)
    return fake_redis


async def test_module_helpers_open_the_connection(sink_redis) -> None:
    written = await notifications_sink.record_notification("hello", recipient="team")

    records = await notifications_sink.read_notifications()

    assert records == [written]
    assert records[0]["message"] == "hello"
    assert records[0]["recipient"] == "team"


async def test_record_notification_defaults_recipient_to_none(sink_redis) -> None:
    await notifications_sink.record_notification("no address")

    records = await notifications_sink.read_notifications()
    assert records[0]["recipient"] is None


async def test_record_notification_stores_full_vocabulary(sink_redis) -> None:
    # Full parity: the sink writer serializes and stores location/sections/header/footer
    # alongside the message, returned by the read doors for the inbox to render.
    from tai42_contract.channels import OptionSection, ReplyOption
    from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind

    location = LocationElement(latitude=51.5, longitude=-0.12, name="HQ")
    section = OptionSection(title="Pick", rows=[ReplyOption(text="Item A")])
    header = MediaItem(kind=MediaKind.IMAGE, url="https://example.com/h.png")

    await notifications_sink.record_notification("we are here", location=location)
    await notifications_sink.record_notification("choose", sections=[section], header=header, footer="ps")

    records = await notifications_sink.read_notifications()
    assert records[1]["location"] == location.model_dump(mode="json")
    assert records[1]["sections"] is None
    assert records[1]["header"] is None
    assert records[1]["footer"] is None
    assert records[0]["sections"] == [section.model_dump(mode="json")]
    assert records[0]["header"] == header.model_dump(mode="json")
    assert records[0]["footer"] == "ps"
    assert records[0]["location"] is None


async def test_record_defaults_full_vocabulary_to_none(fake_redis) -> None:
    # A plain record carries None for every richer-send field — the plain shape.
    sink = NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, _TEST_FEED_TTL)
    record = await sink.record(fake_redis, "plain", None)
    assert record["location"] is None
    assert record["sections"] is None
    assert record["header"] is None
    assert record["footer"] is None


# -- per-identity feed (audience) --------------------------------------------


async def test_record_with_audience_writes_both_feeds(fake_redis) -> None:
    sink = NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, _TEST_FEED_TTL)
    record = await sink.record(fake_redis, "for-alice", None, audience="alice")
    assert record["audience"] == "alice"
    # On the shared feed AND alice's own per-identity feed; a different identity's
    # feed is untouched.
    assert await sink.read(fake_redis) == [record]
    assert await sink.read_for(fake_redis, "alice") == [record]
    assert await sink.read_for(fake_redis, "bob") == []


async def test_broadcast_touches_only_the_shared_feed(fake_redis) -> None:
    sink = NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, _TEST_FEED_TTL)
    rec = await sink.record(fake_redis, "broadcast", None)
    assert rec["audience"] is None
    assert await sink.read(fake_redis) == [rec]
    assert await sink.read_for(fake_redis, "alice") == []


async def test_recipient_and_audience_are_independent(fake_redis) -> None:
    # recipient (a channel delivery ADDRESS) and audience (an IDENTITY) coexist on
    # one record — the address is never reused as the feed key.
    sink = NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, _TEST_FEED_TTL)
    rec = await sink.record(fake_redis, "hi", "+15550000000", audience="alice")
    assert rec["recipient"] == "+15550000000"
    assert rec["audience"] == "alice"
    on_feed = await sink.read_for(fake_redis, "alice")
    assert on_feed == [rec]
    # No feed is keyed on the recipient address.
    assert await sink.read_for(fake_redis, "+15550000000") == []


async def test_audience_feed_key_is_scoped_by_prefix(fake_redis) -> None:
    sink = NotificationSink("tenant-b:", _TEST_FEED_MAX, _TEST_FEED_TTL)
    await sink.record(fake_redis, "scoped", None, audience="alice")
    assert "tenant-b:notifications:audience:alice" in fake_redis._lists


async def test_audience_feed_evicts_beyond_the_cap(fake_redis) -> None:
    # The per-identity feed is its own bounded ring, mirroring the shared feed.
    cap = 3
    sink = NotificationSink(_DEFAULT_PREFIX, cap, _TEST_FEED_TTL)
    written = [await sink.record(fake_redis, f"m-{i}", None, audience="alice") for i in range(cap + 2)]
    assert await sink.read_for(fake_redis, "alice") == list(reversed(written[-cap:]))


async def test_audience_feed_key_expires_but_shared_feed_does_not(fake_redis) -> None:
    # The per-identity feed key is minted one-per-distinct-identity and read
    # non-destructively, so it carries a rolling TTL to keep it from accumulating
    # forever; the shared feed carries NONE (a TTL there could drop the operator
    # inbox after a quiet period).
    ttl = 100
    sink = NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, ttl)
    await sink.record(fake_redis, "for-alice", None, audience="alice")

    audience_key = f"{_DEFAULT_PREFIX}notifications:audience:alice"
    shared_key = f"{_DEFAULT_PREFIX}notifications:feed"
    # The push set an expiry on the per-identity key, none on the shared feed.
    assert audience_key in fake_redis._ttls
    assert shared_key not in fake_redis._ttls

    # Past the TTL the per-identity key is reclaimed; the shared feed still stands.
    fake_redis.advance(ttl + 1)
    assert await sink.read_for(fake_redis, "alice") == []
    assert len(await sink.read(fake_redis)) == 1


async def test_audience_feed_ttl_rolls_forward_on_each_push(fake_redis) -> None:
    # Each push refreshes the per-identity key's expiry, so a steadily-addressed
    # identity's feed never expires out from under it.
    ttl = 100
    sink = NotificationSink(_DEFAULT_PREFIX, _TEST_FEED_MAX, ttl)
    await sink.record(fake_redis, "first", None, audience="alice")
    fake_redis.advance(ttl - 1)
    await sink.record(fake_redis, "second", None, audience="alice")
    # Past the FIRST push's expiry but within the refreshed one: both records stand.
    fake_redis.advance(2)
    assert len(await sink.read_for(fake_redis, "alice")) == 2


async def test_module_writer_sets_the_audience_ttl(monkeypatch, fake_redis) -> None:
    # The module writer builds the sink with the configured TTL, so a real
    # ``record_notification`` push sets the per-identity key's expiry.
    settings = InteractionsSettings(notifications_feed_ttl_seconds=123)

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(notifications_sink, "client_ctx", _ctx)
    monkeypatch.setattr(notifications_sink, "interactions_settings", lambda: settings)

    await notifications_sink.record_notification("for-alice", audience="alice")

    audience_key = f"{settings.key_prefix}notifications:audience:alice"
    shared_key = f"{settings.key_prefix}notifications:feed"
    assert fake_redis._ttls[audience_key] == fake_redis.time + 123
    assert shared_key not in fake_redis._ttls


async def test_read_notifications_audience_routes_to_per_identity_feed(sink_redis) -> None:
    broadcast = await notifications_sink.record_notification("broadcast")
    addressed = await notifications_sink.record_notification("for-alice", audience="alice")
    # The per-identity read returns ONLY alice's addressed record…
    assert await notifications_sink.read_notifications(audience="alice") == [addressed]
    # …while the shared read returns everything, newest-first.
    assert await notifications_sink.read_notifications() == [addressed, broadcast]


async def test_feed_key_is_scoped_by_the_interactions_key_prefix(monkeypatch, fake_redis) -> None:
    # Two deployments sharing one interactions Redis under distinct
    # INTERACTIONS_KEY_PREFIX values must not collide on a single global feed. The
    # sink threads the interactions key_prefix into its feed key, so a non-default
    # prefix must appear in the actual Redis key written — and the un-prefixed
    # ``notifications:feed`` must NOT. A hardcoded feed key fails this.
    settings = InteractionsSettings(key_prefix="tenant-b:")

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(notifications_sink, "client_ctx", _ctx)
    monkeypatch.setattr(notifications_sink, "interactions_settings", lambda: settings)

    await notifications_sink.record_notification("scoped")

    assert set(fake_redis._lists) == {"tenant-b:notifications:feed"}
    stored = json.loads(fake_redis._lists["tenant-b:notifications:feed"][0])
    assert stored["message"] == "scoped"
