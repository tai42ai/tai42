"""The single-use pair-code store — mint/rotate, the atomic redeem burn, TTL expiry, the
uniform miss, and sha256-at-rest — against the faked redis string + Lua seam."""

from __future__ import annotations

import asyncio
import re

import pytest
from tai42_contract.conversations import PairCodeInvalidError
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.conversations import pair_codes as pair_codes_module
from tai42_skeleton.conversations.pair_codes import _INVALID_CODE, ConversationPairCodeStore, MintingConversation
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_CODE_RE = re.compile(r"^LINK-[A-Z0-9]{8}$")


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")


def _store(monkeypatch, fake: FakeRecordRedis) -> ConversationPairCodeStore:
    monkeypatch.setattr(pair_codes_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationPairCodeStore(ConversationsSettings())


def _conversation(**over) -> MintingConversation:
    base = {
        "target_kind": "agent",
        "target_name": "assistant",
        "route_name": "line-a",
        "door": "channel",
        "channel": "twilio",
        "our_identity": "+15550001111",
        "address": "+15550002222",
    }
    base.update(over)
    return MintingConversation(**base)  # type: ignore[arg-type]


def _pin_codes(monkeypatch, *codes: str) -> None:
    seq = iter(codes)
    monkeypatch.setattr(pair_codes_module, "_generate_code", lambda: next(seq))


# -- mint / redeem round trip --------------------------------------------------


@pytest.mark.asyncio
async def test_mint_returns_well_formed_code_and_expiry(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)

    code, _expires_at = await store.mint(_conversation())
    assert _CODE_RE.match(code)
    settings = ConversationsSettings()
    assert fake.ttl_ms[settings.pair_code_key(hash_api_key(code))] == settings.pair_code_ttl_seconds * 1000


@pytest.mark.asyncio
async def test_redeem_returns_the_full_minting_conversation(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    conversation = _conversation(route_name="line-xyz", address="+19998887777")

    code, _ = await store.mint(conversation)
    redeemed = await store.redeem(code)
    assert redeemed == conversation
    assert redeemed.route_name == "line-xyz"


@pytest.mark.asyncio
async def test_redeem_burns_single_use(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)

    code, _ = await store.mint(_conversation())
    await store.redeem(code)
    with pytest.raises(PairCodeInvalidError) as exc:
        await store.redeem(code)
    # The already-redeemed reply is byte-identical to the unknown/expired one (no oracle).
    assert str(exc.value) == _INVALID_CODE


# -- rotation ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_mint_rotates_and_invalidates_the_first(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    _pin_codes(monkeypatch, "LINK-AAAAAAAA", "LINK-BBBBBBBB")

    first, _ = await store.mint(_conversation())
    second, _ = await store.mint(_conversation())
    assert first != second

    with pytest.raises(PairCodeInvalidError):
        await store.redeem(first)
    # The newest code still redeems.
    assert (await store.redeem(second)).target_name == "assistant"


@pytest.mark.asyncio
async def test_rotation_is_scoped_per_conversation(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    _pin_codes(monkeypatch, "LINK-AAAAAAAA", "LINK-BBBBBBBB")

    # Two DIFFERENT minting conversations each keep their own open code; neither rotates the
    # other out.
    code_a, _ = await store.mint(_conversation(address="+1111"))
    code_b, _ = await store.mint(_conversation(address="+2222"))
    assert (await store.redeem(code_a)).address == "+1111"
    assert (await store.redeem(code_b)).address == "+2222"


# -- concurrency ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_double_redeem_admits_one_winner(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    code, _ = await store.mint(_conversation())

    async def _try() -> object:
        try:
            return await store.redeem(code)
        except PairCodeInvalidError as exc:
            return exc

    left, right = await asyncio.gather(_try(), _try())
    winners = [r for r in (left, right) if isinstance(r, MintingConversation)]
    losers = [r for r in (left, right) if isinstance(r, PairCodeInvalidError)]
    assert len(winners) == 1
    assert len(losers) == 1


# -- TTL + uniform miss --------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_code_redeems_as_invalid(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()

    code, _ = await store.mint(_conversation())
    fake.advance(settings.pair_code_ttl_seconds + 1)
    with pytest.raises(PairCodeInvalidError) as exc:
        await store.redeem(code)
    # The expired reply is byte-identical to the unknown/already-redeemed one (no oracle).
    assert str(exc.value) == _INVALID_CODE


@pytest.mark.asyncio
async def test_unknown_code_redeems_as_invalid(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    with pytest.raises(PairCodeInvalidError) as exc:
        await store.redeem("LINK-ZZZZZZZZ")
    # The unknown reply is byte-identical to the expired/already-redeemed one (no oracle).
    assert str(exc.value) == _INVALID_CODE


@pytest.mark.asyncio
async def test_mint_raises_after_two_entropy_collisions(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    # Pin every generated code to one already present in the keyspace, so both mint attempts
    # collide and the store raises rather than looping.
    monkeypatch.setattr(pair_codes_module, "_generate_code", lambda: "LINK-CCCCCCCC")
    fake._strings[settings.pair_code_key(hash_api_key("LINK-CCCCCCCC"))] = "{}"

    with pytest.raises(RuntimeError):
        await store.mint(_conversation())


# -- sha256-at-rest ------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_code_is_never_stored_under_any_key(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)

    code, _ = await store.mint(_conversation())

    haystack = list(fake._strings) + list(fake._strings.values()) + list(fake._hashes)
    for field_map in fake._hashes.values():
        haystack.extend(field_map)
        haystack.extend(field_map.values())
    # The raw code appears nowhere in the keyspace; only its sha256 keys the record.
    assert not any(code in blob for blob in haystack)
    assert any(hash_api_key(code) in key for key in fake._strings)


# -- 501 without the redis backend ---------------------------------------------


def test_pair_code_store_refuses_without_the_redis_conversations_backend(monkeypatch):
    # A minted code must outlive the minting request in durable state, never per-worker memory,
    # so construction refuses with a loud 501 when no conversations Redis is configured.
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    with pytest.raises(NotSupportedError):
        ConversationPairCodeStore(ConversationsSettings())
