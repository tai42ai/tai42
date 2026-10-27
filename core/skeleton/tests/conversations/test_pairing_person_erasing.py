"""A pairing or message turn that meets a person being erased: the words the end user reads, and what stays."""

from __future__ import annotations

import pytest
from tai42_contract.conversations import ReplyTexts

from tai42_skeleton.conversations import persons as persons_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.persons import _address_key_of
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import pairing as pairing_module

from .conftest import FakeManager, _settle
from .test_pairing import (
    _TARGET,
    EchoAgent,
    FakeChannel,
    _channel_route,
    _mint_via_link,
    _person_store,
    _seed_config,
    _store,
    _wire,
    env,  # noqa: F401  # the pairing suite's fixture
)

_MINTER = "+1000"
_REDEEMER = "+2000"


async def _person_of(address: str, our_identity: str):
    return await _person_store().get_person(
        _TARGET, door="channel", channel="twilio", our_identity=our_identity, address=address
    )


async def _index_of(fake, person) -> dict[str, str]:
    index_key = _person_store().settings.keyspace.person_index_key("agent", "assistant")
    fields = {_address_key_of(address) for address in person.addresses}
    return {field: owner for field, owner in (await fake.hgetall(index_key)).items() if field in fields}


def _routes(**reply_texts):
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")
    if reply_texts:
        route_b = route_b.model_copy(update={"reply_texts": ReplyTexts(**reply_texts)})
    return route_a, route_b


async def _send(text: str, provider: str, *, address: str = _REDEEMER, our_identity: str = "+15550009999") -> str:
    mid = await turn_module.accept(
        "twilio",
        our_identity,
        address,
        address,
        text,
        provider,
        medium_message_id=None,
        reply_to_medium_message_id=None,
    )
    await _settle()
    return mid


async def _answered(mid: str) -> str:
    record = await _store().get_record(mid)
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer is not None
    return record.answer


@pytest.mark.parametrize(
    ("reply_texts", "expected"),
    [({}, pairing_module._LINK_UNAVAILABLE_TEXT), ({"pairing_link_unavailable": "Not now."}, "Not now.")],
)
async def test_a_redeem_of_a_code_minted_by_a_marked_person_answers_link_unavailable(
    reply_texts,
    expected,
    env,  # noqa: F811
    monkeypatch,
):
    _seed_config(env)
    route_a, route_b = _routes(**reply_texts)
    code = await _mint_via_link(monkeypatch, env, route_a, address=_MINTER, provider="PID-A")
    minter = await _person_of(_MINTER, "+15550001111")
    assert minter is not None
    await _person_store().mark_erasing(minter.person_id)
    _wire(monkeypatch, FakeManager(route_a, route_b), FakeChannel())

    assert await _answered(await _send(code, "PID-B")) == expected

    assert await _person_store().get_by_id(minter.person_id, include_erasing=True) == minter


async def test_a_redeemer_marked_before_its_merge_answers_link_unavailable_and_the_code_is_spent(
    env,  # noqa: F811
    monkeypatch,
):
    _seed_config(env)
    route_a, route_b = _routes()
    code = await _mint_via_link(monkeypatch, env, route_a, address=_MINTER, provider="PID-A")
    minter = await _person_of(_MINTER, "+15550001111")
    assert minter is not None
    minter_index = await _index_of(env, minter)
    _wire(monkeypatch, FakeManager(route_a, route_b), FakeChannel())
    real_merge = persons_module.ConversationPersonStore.merge

    async def _marked_first(self, person_id_a, person_id_b):
        await self.mark_erasing(person_id_a)
        return await real_merge(self, person_id_a, person_id_b)

    monkeypatch.setattr(persons_module.ConversationPersonStore, "merge", _marked_first)

    assert await _answered(await _send(code, "PID-B")) == pairing_module._LINK_UNAVAILABLE_TEXT

    monkeypatch.setattr(persons_module.ConversationPersonStore, "merge", real_merge)
    third = await _send(code, "PID-C", address="+3000")
    assert await _answered(third) == pairing_module._INVALID_CODE_TEXT
    assert await _person_store().get_by_id(minter.person_id) == minter
    assert await _index_of(env, minter) == minter_index


@pytest.mark.parametrize(
    ("reply_texts", "expected"),
    [({}, outcome_module._ERROR_ANSWER_TEXT), ({"error": "Algo salió mal."}, "Algo salió mal.")],
)
async def test_a_redeemer_erased_before_its_merge_answers_the_error_text_and_the_code_is_spent(
    reply_texts,
    expected,
    env,  # noqa: F811
    monkeypatch,
):
    _seed_config(env)
    route_a, route_b = _routes(**reply_texts)
    code = await _mint_via_link(monkeypatch, env, route_a, address=_MINTER, provider="PID-A")
    minter = await _person_of(_MINTER, "+15550001111")
    assert minter is not None
    minter_index = await _index_of(env, minter)
    _wire(monkeypatch, FakeManager(route_a, route_b), FakeChannel())
    real_merge = persons_module.ConversationPersonStore.merge

    async def _erased_first(self, person_id_a, person_id_b):
        read = await self.read_for_erase(person_id_a)
        assert read is not None
        assert await self.mark_erasing(person_id_a) is True
        await self.erase(read.person, read.absorbed_ids, read.route_threads)
        return await real_merge(self, person_id_a, person_id_b)

    monkeypatch.setattr(persons_module.ConversationPersonStore, "merge", _erased_first)

    record = await _store().get_record(await _send(code, "PID-B"))
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == expected

    monkeypatch.setattr(persons_module.ConversationPersonStore, "merge", real_merge)
    assert await _answered(await _send(code, "PID-B2")) == pairing_module._INVALID_CODE_TEXT
    assert await _person_store().get_by_id(minter.person_id) == minter
    assert await _index_of(env, minter) == minter_index


async def _linked(env, monkeypatch, route_a, route_b):  # noqa: F811
    code = await _mint_via_link(monkeypatch, env, route_a, address=_MINTER, provider="PID-A")
    _wire(monkeypatch, FakeManager(route_a, route_b), FakeChannel())
    assert "linked" in (await _answered(await _send(code, "PID-B"))).lower()
    person = await _person_of(_REDEEMER, "+15550009999")
    assert person is not None
    assert len(person.addresses) == 2
    return person


@pytest.mark.parametrize(
    ("reply_texts", "expected"),
    [({}, pairing_module._UNLINK_UNAVAILABLE_TEXT), ({"pairing_unlink_unavailable": "Not now."}, "Not now.")],
)
async def test_an_unlink_whose_person_is_marked_before_its_detach_answers_unlink_unavailable(
    reply_texts,
    expected,
    env,  # noqa: F811
    monkeypatch,
):
    _seed_config(env)
    route_a, route_b = _routes(**reply_texts)
    person = await _linked(env, monkeypatch, route_a, route_b)
    real_detach = persons_module.ConversationPersonStore.detach

    async def _marked_first(self, person_id, **kwargs):
        await self.mark_erasing(person_id)
        return await real_detach(self, person_id, **kwargs)

    monkeypatch.setattr(persons_module.ConversationPersonStore, "detach", _marked_first)

    answer = await _answered(await _send("/unlink", "PID-U"))

    assert answer == expected
    assert "/link" not in answer
    assert await _person_store().get_by_id(person.person_id, include_erasing=True) == person


@pytest.mark.parametrize(
    ("reply_texts", "expected"),
    [({}, outcome_module._ERROR_ANSWER_TEXT), ({"error": "Algo salió mal."}, "Algo salió mal.")],
)
async def test_an_unlink_from_a_marked_persons_address_answers_the_error_text_and_keeps_the_address(
    reply_texts,
    expected,
    env,  # noqa: F811
    monkeypatch,
):
    _seed_config(env)
    route_a, route_b = _routes(**reply_texts)
    person = await _linked(env, monkeypatch, route_a, route_b)
    index = await _index_of(env, person)
    await _person_store().mark_erasing(person.person_id)

    record = await _store().get_record(await _send("/unlink", "PID-U"))

    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == expected
    assert await _index_of(env, person) == index


async def test_a_message_from_a_marked_persons_address_takes_the_error_outcome_and_runs_no_turn(
    env,  # noqa: F811
    monkeypatch,
):
    _seed_config(env)
    route_a, route_b = _routes()
    person = await _linked(env, monkeypatch, route_a, route_b)
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(route_a, route_b), FakeChannel(), agent)
    persons = _person_store()
    await persons.mark_erasing(person.person_id)

    record = await _store().get_record(await _send("hello", "PID-M"))

    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
    assert record.error is not None
    assert person.person_id in record.error
    assert agent.calls == []
    assert await persons.get_by_id(person.person_id, include_erasing=True) == person

    read = await persons.read_for_erase(person.person_id)
    assert read is not None
    assert read.person is not None
    await persons.erase(read.person, read.absorbed_ids, read.route_threads)
    again = await _store().get_record(await _send("hello again", "PID-M2"))
    assert again is not None
    assert again.answer == "echo: hello again"
    fresh = await _person_of(_REDEEMER, "+15550009999")
    assert fresh is not None
    assert fresh.person_id != person.person_id
    assert [a.address for a in fresh.addresses] == [_REDEEMER]
