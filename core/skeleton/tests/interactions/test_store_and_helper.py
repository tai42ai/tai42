"""Behavior: a write -> read -> mark cycle against the fake pooled redis, and the
blocking ``ask_user`` helper resolving when an answer lands on the reply channel.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionRequest,
    InteractionResponse,
    MediaItem,
    MediaKind,
)
from tai42_contract.secrets import SecretValue

from tai42_skeleton.interactions import InteractionStore, ask_user
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.origin import reset_interaction_origin, set_interaction_origin
from tests._helpers import await_add_event


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # the interactions surface is OFF with no Redis. These tests exercise the ON
    # feature, so configure its store — the fake connection still stands in; only the
    # presence gate reads this env var.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


def _request(interaction_id: str, group_id: str, store: InteractionStore) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=interaction_id,
        group_id=group_id,
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(interaction_id),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
    )


async def test_audience_round_trips_through_store(fake_redis):
    # The ``audience`` identity rides the persisted contract model: add -> get_state
    # and the backlog both preserve it, and it defaults to None (unaddressed) when
    # the field is absent from the stored record.
    store = InteractionStore("t:")
    addressed = _request("i1", "g1", store).model_copy(update={"audience": "alice"})
    unaddressed = _request("i2", "g2", store)

    await store.add(fake_redis, addressed, idle_ttl=100)
    await store.add(fake_redis, unaddressed, idle_ttl=100)

    got = await store.get_state(fake_redis, "i1")
    assert got is not None
    assert got.request.audience == "alice"
    got_none = await store.get_state(fake_redis, "i2")
    assert got_none is not None
    assert got_none.request.audience is None

    backlog = {req.interaction_id: req.audience for req in await store.backlog(fake_redis)}
    assert backlog == {"i1": "alice", "i2": None}


async def test_media_round_trips_through_store(fake_redis):
    # Display-only media rides the persisted contract model: the store serializes and
    # deserializes it generically (model_dump_json write -> model_validate_json read),
    # so add -> get_state and the backlog both preserve it, and it defaults to None
    # when the field is absent.
    store = InteractionStore("t:")
    media = [
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/p.png", caption="A product"),
        MediaItem(kind=MediaKind.LINK, url="https://docs.example/p"),
    ]
    with_media = _request("i1", "g1", store).model_copy(update={"media": media})
    without_media = _request("i2", "g2", store)

    await store.add(fake_redis, with_media, idle_ttl=100)
    await store.add(fake_redis, without_media, idle_ttl=100)

    got = await store.get_state(fake_redis, "i1")
    assert got is not None
    assert got.request.media == media
    got_none = await store.get_state(fake_redis, "i2")
    assert got_none is not None
    assert got_none.request.media is None

    backlog = {req.interaction_id: req.media for req in await store.backlog(fake_redis)}
    assert backlog == {"i1": media, "i2": None}


async def test_write_read_mark_cycle(fake_redis):
    store = InteractionStore("t:")
    request = _request("i1", "g1", store)

    await store.add(fake_redis, request, idle_ttl=100)

    pending = await store.get_state(fake_redis, "i1")
    assert pending is not None
    assert pending.status == "pending"
    assert pending.group_id == "g1"
    assert pending.request == request

    response = InteractionResponse(
        interaction_id="i1",
        answer="go",
        answered_by="tester",
        answered_at=datetime.now(UTC),
    )
    claimed = await store.record_answer(fake_redis, response, "g1", reply_ttl=60)
    assert claimed is True

    answered = await store.get_state(fake_redis, "i1")
    assert answered is not None
    assert answered.status == "answered"
    assert answered.response is not None
    assert answered.response.answer == "go"

    # A duplicate answer is a lost race: nothing claimed, nothing re-pushed.
    again = await store.record_answer(fake_redis, response, "g1", reply_ttl=60)
    assert again is False

    # The first answer is waiting on the reply channel for a blocked caller.
    delivered = await store.wait_for_reply(fake_redis, store.reply_key("i1"), timeout_seconds=1, grace_seconds=5)
    assert delivered is not None
    assert delivered.answer == "go"


async def test_ask_user_blocks_until_answer(monkeypatch, fake_redis, fake_client_ctx):
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    store = InteractionStore(helper_module.interactions_settings().key_prefix)

    async def answer_when_asked() -> None:
        interaction_id, group_id = await await_add_event(fake_redis, store)
        await store.record_answer(
            fake_redis,
            InteractionResponse(
                interaction_id=interaction_id,
                answer="hello human",
                answered_by="tester",
                answered_at=datetime.now(UTC),
            ),
            group_id,
            reply_ttl=60,
        )

    answerer = asyncio.create_task(answer_when_asked())
    result = await ask_user("anything?", timeout=5)
    await answerer

    assert result == "hello human"


async def test_ask_user_sensitive_returns_secret_value(monkeypatch, fake_redis, fake_client_ctx):
    # A sensitive ask hands the caller the answer WRAPPED: the real value is reached
    # only through ``reveal()``, and repr/str never expose it.
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    store = InteractionStore(helper_module.interactions_settings().key_prefix)

    async def answer_when_asked() -> None:
        interaction_id, group_id = await await_add_event(fake_redis, store)
        await store.record_answer(
            fake_redis,
            InteractionResponse(
                interaction_id=interaction_id,
                answer="hunter2",
                answered_by="tester",
                answered_at=datetime.now(UTC),
            ),
            group_id,
            reply_ttl=60,
        )

    answerer = asyncio.create_task(answer_when_asked())
    result = await ask_user("your password?", timeout=5, sensitive=True)
    await answerer

    assert isinstance(result, SecretValue)
    assert result.reveal() == "hunter2"
    # The real answer never leaks through the wrapper's repr.
    assert "hunter2" not in repr(result)


async def _answer_and_report_origin(fake_redis, store) -> str | None:
    # Play the answerer: wake the blocked caller and report the origin stamped on
    # the persisted question record.
    interaction_id, group_id = await await_add_event(fake_redis, store)
    state = await store.get_state(fake_redis, interaction_id)
    assert state is not None
    await store.record_answer(
        fake_redis,
        InteractionResponse(
            interaction_id=interaction_id,
            answer="ok",
            answered_by="tester",
            answered_at=datetime.now(UTC),
        ),
        group_id,
        reply_ttl=60,
    )
    return state.request.origin


async def test_ask_user_stamps_origin_from_bound_context(monkeypatch, fake_redis, fake_client_ctx):
    # A question raised inside a bound run carries that run's origin on its durable record.
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    store = InteractionStore(helper_module.interactions_settings().key_prefix)

    token = set_interaction_origin("run-42")
    try:
        answerer = asyncio.create_task(_answer_and_report_origin(fake_redis, store))
        await ask_user("bound?", timeout=5)
        assert await answerer == "run-42"
    finally:
        reset_interaction_origin(token)


async def test_ask_user_origin_none_outside_bound_context(monkeypatch, fake_redis, fake_client_ctx):
    # Outside any bound run the origin is None (an unattributed ask).
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    store = InteractionStore(helper_module.interactions_settings().key_prefix)

    answerer = asyncio.create_task(_answer_and_report_origin(fake_redis, store))
    await ask_user("unbound?", timeout=5)
    assert await answerer is None


async def test_ask_user_times_out(monkeypatch, fake_client_ctx):
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    with pytest.raises(helper_module.InteractionTimeoutError):
        await ask_user("no one answers", timeout=0.05)


@pytest.mark.parametrize("audience", ["", "  "])
async def test_ask_user_blank_audience_raises(audience):
    # A blank/whitespace audience can never address a real identity — rejected loudly
    # up front (mirroring notify_user), before any state is written.
    with pytest.raises(ValueError, match="audience must be a non-empty identity"):
        await ask_user("anything?", audience=audience)


async def test_ask_user_zero_timeout_raises_before_redis(monkeypatch):
    # Redis BLPOP treats 0 as "block forever", so a non-positive budget must
    # raise ValueError up front — before any redis connection is opened.
    calls: list = []

    @asynccontextmanager
    async def tracking_ctx(*args, **kwargs):
        calls.append((args, kwargs))
        yield None

    monkeypatch.setattr(helper_module, "client_ctx", tracking_ctx)
    with pytest.raises(ValueError, match="timeout must be positive"):
        await ask_user("too impatient", timeout=0)
    assert calls == []
