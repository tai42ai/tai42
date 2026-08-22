"""Behavior: a write -> read -> mark cycle against the fake pooled redis, and the
blocking ``ask_user`` helper resolving when an answer lands on the reply channel.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import (
    MEDIA_ROUTE_PREFIX,
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

from .._helpers import await_add_event


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
    # and the pending list both preserve it, and it defaults to None (unaddressed) when
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

    pending = {req.interaction_id: req.audience for req in await store.pending(fake_redis)}
    assert pending == {"i1": "alice", "i2": None}


async def test_answered_removed_events_carry_audience(fake_redis):
    # The answered/removed events carry the question's audience so the tail-only SSE
    # filters those frames directly (no record left to read). An addressed question's
    # event names its audience; an unaddressed one omits the field (None).
    store = InteractionStore("t:")
    addressed = _request("i1", "g1", store).model_copy(update={"audience": "alice"})
    unaddressed = _request("i2", "g2", store)
    await store.add(fake_redis, addressed, idle_ttl=100)
    await store.add(fake_redis, unaddressed, idle_ttl=100)

    await store.record_answer(
        fake_redis,
        InteractionResponse(interaction_id="i1", answer="x", answered_by="t", answered_at=datetime.now(UTC)),
        "g1",
        reply_ttl=60,
    )
    assert await store.prune_pending(fake_redis, "i2", "g2") == "pruned"

    events = await fake_redis.xrange(store.events_key)
    terminal = {
        f["interaction_id"]: f for _id, f in events if f["type"] in ("interaction.answered", "interaction.removed")
    }
    assert terminal["i1"]["type"] == "interaction.answered"
    assert terminal["i1"]["audience"] == "alice"
    assert terminal["i2"]["type"] == "interaction.removed"
    assert "audience" not in terminal["i2"]  # unaddressed -> no audience field


async def test_add_indexes_media_and_extends_to_group_horizon(fake_redis):
    # A question referencing served media joins the group's media index, and its media
    # key TTL is set-or-extended to the group horizon so the bytes never expire before
    # the durable record that points at them.
    store = InteractionStore("t:")
    media_id = "z" * 43
    key = store.media_key(media_id)
    await fake_redis.hset(key, mapping={"mime": "image/png", "b64": "AA=="})
    fake_redis._expire(key, 10)  # a short bootstrap TTL, as substitute_media sets
    ref = MediaItem(kind=MediaKind.IMAGE, url=MEDIA_ROUTE_PREFIX + media_id)
    req = _request("i1", "g1", store).model_copy(update={"media": [ref]})

    await store.add(fake_redis, req, idle_ttl=100)

    assert media_id in await fake_redis.smembers(store.media_index_key("g1"))
    # The media key TTL rose from the bootstrap 10 to the group horizon (idle_ttl 100).
    assert await fake_redis.ttl(key) == 100
    assert await fake_redis.ttl(store.media_index_key("g1")) == 100


async def test_answer_drops_media_from_index_leaving_key_to_ttl(fake_redis):
    # A terminal answer removes the question's media ids from the group's media index so
    # the index cannot grow without bound; the media key itself is left to expire on its
    # own TTL (removal/answer paths never delete media).
    store = InteractionStore("t:")
    media_id = "z" * 43
    key = store.media_key(media_id)
    await fake_redis.hset(key, mapping={"mime": "image/png", "b64": "AA=="})
    ref = MediaItem(kind=MediaKind.IMAGE, url=MEDIA_ROUTE_PREFIX + media_id)
    req = _request("i1", "g1", store).model_copy(update={"media": [ref]})
    await store.add(fake_redis, req, idle_ttl=100)
    assert media_id in await fake_redis.smembers(store.media_index_key("g1"))

    await store.record_answer(
        fake_redis,
        InteractionResponse(interaction_id="i1", answer="x", answered_by="t", answered_at=datetime.now(UTC)),
        "g1",
        reply_ttl=60,
    )

    assert media_id not in await fake_redis.smembers(store.media_index_key("g1"))
    # The media key survives — left to expire on its group TTL, never deleted on answer.
    assert await fake_redis.ttl(key) == 100


async def test_prune_drops_media_from_index_leaving_key_to_ttl(fake_redis):
    # The prune terminal path mirrors the answer path: the abandoned question's media ids
    # leave the group index, the media key is left to its own TTL.
    store = InteractionStore("t:")
    media_id = "y" * 43
    key = store.media_key(media_id)
    await fake_redis.hset(key, mapping={"mime": "image/png", "b64": "AA=="})
    ref = MediaItem(kind=MediaKind.IMAGE, url=MEDIA_ROUTE_PREFIX + media_id)
    req = _request("i1", "g1", store).model_copy(update={"media": [ref]})
    await store.add(fake_redis, req, idle_ttl=100)

    assert await store.prune_pending(fake_redis, "i1", "g1") == "pruned"

    assert media_id not in await fake_redis.smembers(store.media_index_key("g1"))
    assert await fake_redis.ttl(key) == 100  # media key left to expire by TTL


async def test_no_media_add_extends_a_prior_questions_media_ttl(fake_redis):
    # A later co-grouped question with NO media of its own still extends every existing
    # member media key to the group's new (longer) horizon, so a long-lived group never
    # loses the bytes an earlier question points at.
    store = InteractionStore("t:")
    media_id = "w" * 43
    key = store.media_key(media_id)
    await fake_redis.hset(key, mapping={"mime": "image/png", "b64": "AA=="})
    ref = MediaItem(kind=MediaKind.IMAGE, url=MEDIA_ROUTE_PREFIX + media_id)
    q1 = _request("i1", "g1", store).model_copy(update={"media": [ref]})
    await store.add(fake_redis, q1, idle_ttl=100)
    assert await fake_redis.ttl(key) == 100

    # A second question in the same group with a longer horizon and no media.
    q2 = _request("i2", "g1", store)
    await store.add(fake_redis, q2, idle_ttl=200)

    assert await fake_redis.ttl(key) == 200  # q2's add raised q1's media TTL to the new horizon


async def test_add_denormalizes_media_ids_onto_the_state_hash(fake_redis):
    # A question's served-media ids ride the state hash as a comma-joined ``media_ids``
    # field, so a terminal claim drops them from the group index without deserializing the
    # request. A question with no stored media writes no field at all.
    store = InteractionStore("t:")
    id_a, id_b = "a" * 43, "b" * 43
    refs = [
        MediaItem(kind=MediaKind.IMAGE, url=MEDIA_ROUTE_PREFIX + id_a),
        MediaItem(kind=MediaKind.IMAGE, url=MEDIA_ROUTE_PREFIX + id_b),
    ]
    with_media = _request("i1", "g1", store).model_copy(update={"media": refs})
    without_media = _request("i2", "g2", store)

    await store.add(fake_redis, with_media, idle_ttl=100)
    await store.add(fake_redis, without_media, idle_ttl=100)

    assert await fake_redis.hget(store.state_key("i1"), "media_ids") == f"{id_a},{id_b}"
    assert await fake_redis.hget(store.state_key("i2"), "media_ids") is None


async def test_no_media_add_makes_no_extra_round_trip(fake_redis, monkeypatch):
    # A no-media add issues exactly two server round trips: the phantom-purge eval and the
    # batched write pipeline. The media set-or-extend eval is queued INTO that write
    # pipeline, so it commits with the question and makes no separate SMEMBERS or media
    # round trip of its own.
    store = InteractionStore("t:")
    round_trips = {"n": 0}
    real_eval = fake_redis.eval
    real_smembers = fake_redis.smembers
    real_pipeline = fake_redis.pipeline

    async def counting_eval(*a, **k):
        round_trips["n"] += 1
        return await real_eval(*a, **k)

    async def counting_smembers(*a, **k):
        round_trips["n"] += 1
        return await real_smembers(*a, **k)

    def counting_pipeline():
        pipe = real_pipeline()
        real_execute = pipe.execute

        async def counting_execute():
            round_trips["n"] += 1
            return await real_execute()

        pipe.execute = counting_execute
        return pipe

    monkeypatch.setattr(fake_redis, "eval", counting_eval)
    monkeypatch.setattr(fake_redis, "smembers", counting_smembers)
    monkeypatch.setattr(fake_redis, "pipeline", counting_pipeline)

    await store.add(fake_redis, _request("i1", "g1", store), idle_ttl=100)

    assert round_trips["n"] == 2


async def test_media_round_trips_through_store(fake_redis):
    # Display-only media rides the persisted contract model: the store serializes and
    # deserializes it generically (model_dump_json write -> model_validate_json read),
    # so add -> get_state and the pending list both preserve it, and it defaults to None
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

    pending = {req.interaction_id: req.media for req in await store.pending(fake_redis)}
    assert pending == {"i1": media, "i2": None}


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
