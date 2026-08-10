"""The per-target person registry — provisional get-or-create, transitive merge with the
store-decided survivor, detach, and route accumulation — against the faked redis hash + string
+ Lua seam."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.conversations import CrossTargetMergeError, NotLinkedError, Person, PersonAddress

from tai42_skeleton.conversations import persons as persons_module
from tai42_skeleton.conversations.persons import ConversationPersonStore, PairingTarget, _door_address_key
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")


def _store(monkeypatch, fake: FakeRecordRedis) -> ConversationPersonStore:
    monkeypatch.setattr(persons_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationPersonStore(ConversationsSettings())


_TARGET = PairingTarget(target_kind="agent", target_name="assistant")


async def _channel_person(store: ConversationPersonStore, address: str):
    return await store.get_person(
        _TARGET, door="channel", channel="twilio", our_identity="+15550001111", address=address
    )


def _addr(
    address: str,
    *,
    door: str = "channel",
    routes: tuple[str, ...] = ("line",),
    channel: str | None = "twilio",
    our_identity: str | None = "+15550001111",
) -> PersonAddress:
    return PersonAddress(
        door=door,  # type: ignore[arg-type]
        routes=list(routes),
        channel=channel if door == "channel" else None,
        our_identity=our_identity if door == "channel" else None,
        address=address,
        linked_at=datetime.now(UTC),
    )


def _seed_person(fake: FakeRecordRedis, person: Person) -> None:
    """Write a person row + its index entries straight into the fake, so a test can pin a
    created_at the store's own ``datetime.now`` would not let it choose."""
    settings = ConversationsSettings()
    fake._strings[settings.person_key(person.person_id)] = person.model_dump_json()
    index_key = settings.person_index_key(person.target_kind, person.target_name)
    for address in person.addresses:
        dak = _door_address_key(
            door=address.door, channel=address.channel, our_identity=address.our_identity, address=address.address
        )
        fake._hashes.setdefault(index_key, {})[dak] = person.person_id


def _person(person_id: str, addresses: list[PersonAddress], *, created_at: datetime | None = None) -> Person:
    return Person(
        person_id=person_id,
        target_kind=_TARGET.target_kind,
        target_name=_TARGET.target_name,
        created_at=created_at or datetime.now(UTC),
        addresses=addresses,
    )


# -- provisional get-or-create -------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_provisional_first_contact_creates(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)

    person, created = await store.ensure_provisional(_TARGET, _addr("+15550002222"))
    assert created is True
    assert len(person.addresses) == 1

    again, created_again = await store.ensure_provisional(_TARGET, _addr("+15550002222"))
    assert created_again is False
    assert again.person_id == person.person_id


@pytest.mark.asyncio
async def test_ensure_provisional_race_creates_exactly_one(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)

    first, second = await asyncio.gather(
        store.ensure_provisional(_TARGET, _addr("+15550003333")),
        store.ensure_provisional(_TARGET, _addr("+15550003333")),
    )
    assert {first[1], second[1]} == {True, False}
    assert first[0].person_id == second[0].person_id


@pytest.mark.asyncio
async def test_get_person_miss_then_hit(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)

    assert await _channel_person(store, "x") is None
    created, _ = await store.ensure_provisional(_TARGET, _addr("x"))
    found = await _channel_person(store, "x")
    assert found is not None
    assert found.person_id == created.person_id


@pytest.mark.asyncio
async def test_api_door_address_is_encoded_without_channel_identity(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    api = _addr("svc/enduser", door="api", routes=("api-route",))

    person, created = await store.ensure_provisional(_TARGET, api)
    assert created is True
    assert person.addresses[0].channel is None
    assert person.addresses[0].our_identity is None
    found = await store.get_person(_TARGET, door="api", channel=None, our_identity=None, address="svc/enduser")
    assert found is not None
    assert found.person_id == person.person_id


# -- routes accumulation -------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_provisional_accumulates_routes_without_duplicates(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)

    await store.ensure_provisional(_TARGET, _addr("+1999", routes=("line-a",)))
    person, created = await store.ensure_provisional(_TARGET, _addr("+1999", routes=("line-b",)))
    assert created is False
    assert person.addresses[0].routes == ["line-a", "line-b"]

    # A repeat of a known route appends nothing.
    again, _ = await store.ensure_provisional(_TARGET, _addr("+1999", routes=("line-a",)))
    assert again.addresses[0].routes == ["line-a", "line-b"]


# -- merge ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_unions_addresses_and_rewrites_index(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    older = _person("p-older", [_addr("+1000")], created_at=datetime.now(UTC) - timedelta(minutes=5))
    newer = _person("p-newer", [_addr("+2000")], created_at=datetime.now(UTC))
    _seed_person(fake, older)
    _seed_person(fake, newer)

    survivor = await store.merge("p-older", "p-newer")
    assert survivor.person_id == "p-older"
    assert {a.address for a in survivor.addresses} == {"+1000", "+2000"}
    # Both addresses now resolve to the survivor, and the absorbed row is gone.
    settings = ConversationsSettings()
    assert settings.person_key("p-newer") not in fake._strings
    for address in ("+1000", "+2000"):
        found = await _channel_person(store, address)
        assert found is not None
        assert found.person_id == "p-older"


@pytest.mark.asyncio
async def test_merge_is_order_independent_and_picks_earliest_created(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    older = _person("p-b", [_addr("+1000")], created_at=datetime.now(UTC) - timedelta(minutes=5))
    newer = _person("p-a", [_addr("+2000")], created_at=datetime.now(UTC))
    _seed_person(fake, older)
    _seed_person(fake, newer)

    forward = await store.merge("p-b", "p-a")
    assert forward.person_id == "p-b"  # earlier created_at wins regardless of id ordering

    # Rebuild and merge the other way — same survivor.
    fake2 = FakeRecordRedis()
    store2 = _store(monkeypatch, fake2)
    _seed_person(fake2, older)
    _seed_person(fake2, newer)
    backward = await store2.merge("p-a", "p-b")
    assert backward.person_id == "p-b"


@pytest.mark.asyncio
async def test_merge_ties_break_on_smaller_person_id(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    when = datetime.now(UTC)
    _seed_person(fake, _person("p-zzz", [_addr("+1000")], created_at=when))
    _seed_person(fake, _person("p-aaa", [_addr("+2000")], created_at=when))

    survivor = await store.merge("p-zzz", "p-aaa")
    assert survivor.person_id == "p-aaa"


@pytest.mark.asyncio
async def test_three_way_transitive_merge_converges_to_one(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    now = datetime.now(UTC)
    _seed_person(fake, _person("p-a", [_addr("+1000")], created_at=now - timedelta(minutes=2)))
    _seed_person(fake, _person("p-b", [_addr("+2000")], created_at=now - timedelta(minutes=1)))
    _seed_person(fake, _person("p-c", [_addr("+3000")], created_at=now))

    # A↔B, then (whoever now holds B's address)↔C — the redeem side always merges the
    # CURRENT persons, never a stale absorbed id.
    await store.merge("p-a", "p-b")
    holder_of_b = await store.get_person(
        _TARGET, door="channel", channel="twilio", our_identity="+15550001111", address="+2000"
    )
    assert holder_of_b is not None
    survivor = await store.merge(holder_of_b.person_id, "p-c")
    assert survivor.person_id == "p-a"
    assert {a.address for a in survivor.addresses} == {"+1000", "+2000", "+3000"}
    for address in ("+1000", "+2000", "+3000"):
        found = await _channel_person(store, address)
        assert found is not None
        assert found.person_id == "p-a"


@pytest.mark.asyncio
async def test_merge_same_person_is_a_noop(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    _seed_person(fake, _person("p-solo", [_addr("+1000"), _addr("+2000")]))

    survivor = await store.merge("p-solo", "p-solo")
    assert survivor.person_id == "p-solo"
    assert {a.address for a in survivor.addresses} == {"+1000", "+2000"}


@pytest.mark.asyncio
async def test_merge_across_targets_refuses(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    on_target = _person("p-agent", [_addr("+1000")])
    _seed_person(fake, on_target)
    # A person on a DIFFERENT target, seeded by hand.
    other = Person(
        person_id="p-tool",
        target_kind="tool",
        target_name="ping",
        created_at=datetime.now(UTC),
        addresses=[_addr("+2000")],
    )
    fake._strings[settings.person_key("p-tool")] = other.model_dump_json()

    with pytest.raises(CrossTargetMergeError):
        await store.merge("p-agent", "p-tool")


@pytest.mark.asyncio
async def test_merge_duplicate_address_across_persons_raises(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    # Two persons on the SAME target both claiming +1000 — only reachable from a corrupt
    # per-target index (single-ownership already violated). The address union must surface
    # the corruption, never silently produce a person with a duplicate address.
    _seed_person(fake, _person("p-older", [_addr("+1000")], created_at=datetime.now(UTC) - timedelta(minutes=5)))
    _seed_person(fake, _person("p-newer", [_addr("+1000"), _addr("+2000")], created_at=datetime.now(UTC)))

    with pytest.raises(RuntimeError, match="single-ownership"):
        await store.merge("p-older", "p-newer")


# -- detach --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detach_splits_one_address_into_a_fresh_provisional(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    _seed_person(fake, _person("p-linked", [_addr("+1000", routes=("line-a", "line-b")), _addr("+2000")]))

    fresh = await store.detach(
        "p-linked", door="channel", channel="twilio", our_identity="+15550001111", address="+1000"
    )
    assert fresh.person_id != "p-linked"
    assert [a.address for a in fresh.addresses] == ["+1000"]
    # The detached address kept its accumulated routes (aggregated read still enumerates them).
    assert fresh.addresses[0].routes == ["line-a", "line-b"]

    # The detached address now resolves to the fresh person, and its next inbound is NOT a
    # first-contact creation (the row already exists) — no re-greeting.
    found = await _channel_person(store, "+1000")
    assert found is not None
    assert found.person_id == fresh.person_id
    _, created = await store.ensure_provisional(_TARGET, _addr("+1000"))
    assert created is False

    # The original person keeps the rest.
    remainder = await _channel_person(store, "+2000")
    assert remainder is not None
    assert remainder.person_id == "p-linked"
    assert [a.address for a in remainder.addresses] == ["+2000"]


@pytest.mark.asyncio
async def test_detach_refuses_the_only_address(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    _seed_person(fake, _person("p-solo", [_addr("+1000")]))

    with pytest.raises(NotLinkedError):
        await store.detach("p-solo", door="channel", channel="twilio", our_identity="+15550001111", address="+1000")


@pytest.mark.asyncio
async def test_detach_address_not_on_this_person_raises_loudly(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    _seed_person(fake, _person("p-linked", [_addr("+1000"), _addr("+2000")]))

    # +9999 is not one of this multi-address person's addresses — a caller/id mismatch, NOT
    # the legitimate single-address nothing-to-unlink case. It surfaces as a loud RuntimeError,
    # never the NotLinkedError that would mask the mismatch.
    with pytest.raises(RuntimeError, match="not owned by person"):
        await store.detach("p-linked", door="channel", channel="twilio", our_identity="+15550001111", address="+9999")


@pytest.mark.asyncio
async def test_detach_unknown_person_raises(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    with pytest.raises(RuntimeError):
        await store.detach("p-missing", door="channel", channel="twilio", our_identity="+15550001111", address="+1")


@pytest.mark.asyncio
async def test_merge_unknown_person_raises(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    with pytest.raises(RuntimeError):
        await store.merge("p-missing-a", "p-missing-b")


# -- corruption raises loudly --------------------------------------------------


@pytest.mark.asyncio
async def test_index_naming_a_missing_row_raises_on_lookup_and_ensure(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    # A dangling index entry (no such row) is corruption — both readers raise, never swallow.
    dak = _door_address_key(door="channel", channel="twilio", our_identity="+15550001111", address="+1000")
    fake._hashes.setdefault(settings.person_index_key("agent", "assistant"), {})[dak] = "p-gone"

    with pytest.raises(RuntimeError):
        await _channel_person(store, "+1000")
    with pytest.raises(RuntimeError):
        await store.ensure_provisional(_TARGET, _addr("+1000"))


def test_door_address_key_refuses_mismatched_channel_identity():
    with pytest.raises(ValueError, match="channel address must carry channel and our_identity"):
        _door_address_key(door="channel", channel=None, our_identity="+1", address="+1000")
    with pytest.raises(ValueError, match="api address carries no channel/our_identity"):
        _door_address_key(door="api", channel="twilio", our_identity=None, address="svc/end")


def test_person_store_refuses_without_the_redis_conversations_backend(monkeypatch):
    # A person folded across channels must never live in per-worker state that vanishes with
    # the process, so construction refuses with a loud 501 when no conversations Redis is set.
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    with pytest.raises(NotSupportedError):
        ConversationPersonStore(ConversationsSettings())
