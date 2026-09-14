"""Interactions tail-only SSE stream: live tail, keepalive cadence, SSE resume, and
audience-isolation stream filtering."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import cast

from starlette.requests import Request
from tai42_contract.interactions import AnswerFormat, InteractionResponse

from tai42_skeleton.operations.interactions import _add_data
from tai42_skeleton.routers import interactions as router
from tai42_skeleton.routers.interactions.stream import _CONNECT_FRAME, _stream_events

from ._harness import (
    _EXPECTED_MEDIA_FRAME,
    _MEDIA_ITEMS,
    _add_ids,
    _AliveRequest,
    _collect_stream,
    _event_ids,
    _events_cursor,
    _external_request,
    _frame_fields,
    _identity,
    _is_event,
    _media_request,
    _plain_request,
    _seed,
    _seed_addressed,
    _sensitive_request,
    _tail_add_frames,
    _tail_collect,
    make_request,
)


async def test_stream_route_returns_streaming_response(wired):
    from starlette.responses import StreamingResponse

    resp = await router.stream(make_request("GET"))
    assert isinstance(resp, StreamingResponse)


async def test_stream_cursor_captured_before_response_delivers_later_add(wired):
    # The route handler captures the tail cursor BEFORE returning the response, so an add
    # published after the handler returns — but before the generator's first XREAD — has
    # an id past the cursor and is still delivered. This is the post-headers guarantee: a
    # client holding the response headers never misses a later add.
    from starlette.responses import StreamingResponse

    resp = await router.stream(cast(Request, _AliveRequest(alive=1)))
    assert isinstance(resp, StreamingResponse)
    await _seed(wired, iid="late1", gid="glate")  # published AFTER stream() returned
    frames = [cast(str, frame) async for frame in resp.body_iterator]
    assert _add_ids(frames) == ["late1"]


async def test_stream_flushes_connect_comment_before_tail(wired):
    # The FIRST body byte is a no-op SSE comment flushed at connect — BEFORE the tail's
    # first (blocking) XREAD and therefore before any keepalive interval. This is what lets
    # a fetch-reader client whose Fetch resolves a streamed response only on the first body
    # byte (Firefox; Chromium resolves on the headers) treat the stream as connected in
    # ~0.1s and run its connect-time base refetch now, instead of a full keepalive interval
    # (default 15s) later — the inbox would otherwise be up to 15s stale on every connect.
    # Pulling ONE frame must resolve WITHOUT any XREAD (proving it precedes the tail loop).
    cursor = await _events_cursor(wired)
    xread_calls = {"n": 0}
    real_xread = wired.fake.xread

    async def _counting_xread(streams, block=None):
        xread_calls["n"] += 1
        return await real_xread(streams, block=block)

    wired.monkeypatch.setattr(wired.fake, "xread", _counting_xread)
    gen = _stream_events(cast(Request, _AliveRequest(alive=1)), wired.store, wired.settings, cursor)
    try:
        first = await gen.__anext__()
    finally:
        await gen.aclose()
    assert first == _CONNECT_FRAME
    assert first.startswith(":"), "the connect frame must be an ignorable SSE comment"
    assert xread_calls["n"] == 0, "the connect frame must flush before the tail's first XREAD"


async def test_stream_tail_add_carries_sensitive_flag(wired):
    # The live tail re-fetches the state and forwards the add frame; the sensitive
    # flag must ride that frame too (the question is added after cursor capture).
    async def _inject():
        await wired.store.add(wired.fake, _sensitive_request(wired.store, iid="s7", gid="sg7"), idle_ttl=86400)

    frames = await _tail_collect(wired, _inject)
    add_frames = [f for f in frames if _is_event(f, "interaction.add")]
    assert add_frames, frames
    payload = json.loads(_frame_fields(add_frames[-1])["data"])
    assert payload["interaction_id"] == "s7"
    assert payload["sensitive"] is True


async def test_stream_tail_forwards_add(wired):
    # A pending interaction whose add-event lands after cursor capture is
    # re-fetched and forwarded by the tail.
    async def _inject():
        await _seed(wired, iid="i5", gid="g5")

    frames = await _tail_collect(wired, _inject)
    assert any(_is_event(f, "interaction.add") for f in frames)


async def test_stream_tail_add_carries_media(wired):
    # The live tail re-fetches the pending state and forwards the media on the add frame.
    payloads = await _tail_add_frames(wired, _media_request(wired.store, iid="m7", gid="mg7", media=_MEDIA_ITEMS))
    assert payloads, "no add frame forwarded by the tail"
    assert payloads[-1]["interaction_id"] == "m7"
    assert payloads[-1]["media"] == _EXPECTED_MEDIA_FRAME


async def test_stream_tail_keepalive_then_disconnect(wired):
    # An idle tail whose keepalive deadline is due emits the SSE keepalive comment,
    # and a client disconnect ends the generator instead of blocking forever. A
    # zero window makes the deadline due on the first idle iteration.
    wired.monkeypatch.setattr(router, "_KEEPALIVE_SECONDS", 0)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/s",
        "query_string": b"",
        "headers": [],
        "client": ("1.2.3.4", 1),
    }
    msgs = iter([{}, {"type": "http.disconnect"}])

    async def receive():
        try:
            return next(msgs)
        except StopIteration:
            return {"type": "http.disconnect"}

    cursor = await _events_cursor(wired)
    frames = await _collect_stream(_stream_events(Request(scope, receive), wired.store, wired.settings, cursor))
    # The connect comment flushes first (at connect, before the tail's first XREAD), then
    # the idle window's due keepalive.
    assert frames == [_CONNECT_FRAME, ": keepalive\n\n"]


async def test_keepalive_is_deadline_driven_not_reset_by_filtered_events(wired):
    # A restricted caller's keepalive cadence is independent of other identities'
    # volume, and the keepalive is DEADLINE-driven — not emitted once per window that
    # yields nothing to this caller. Two clock-controlled tail windows, no real waits:
    #   window 1: a bob-addressed add the alice caller filters out lands BEFORE the
    #             armed deadline (clock 1000 -> 1003, deadline 1010). No keepalive may
    #             fire — a per-non-yielding-window keepalive here would leak other
    #             identities' activity timing.
    #   window 2: an idle window crosses the deadline (clock 1003 -> 1011 >= 1010).
    #             EXACTLY ONE keepalive fires — and only because the filtered window
    #             left the 1010 deadline untouched (a reset to 1013 would suppress it).
    # Asserting on the full frame list makes any spurious keepalive visible.
    clock = {"t": 1000.0}
    wired.monkeypatch.setattr(router, "_now", lambda: clock["t"])
    wired.monkeypatch.setattr(router, "_KEEPALIVE_SECONDS", 10)

    real_xread = wired.fake.xread
    advances = iter([3, 8])  # window 1 stays before the deadline; window 2 crosses it
    injected = {"done": False}

    async def _advancing_xread(streams, block=None):
        # A bob-addressed add lands live on the first window (cursor captured an empty
        # tail): window 1's XREAD returns a NON-empty result the restricted alice caller
        # filters out entirely.
        if not injected["done"]:
            injected["done"] = True
            await _seed_addressed(wired, "b1", "gb", "bob")
        result = await real_xread(streams, block=block)
        clock["t"] += next(advances, 0)  # model time elapsed while blocked in this window
        return result

    wired.monkeypatch.setattr(wired.fake, "xread", _advancing_xread)

    with _identity(user_id="keyA", owner="alice"):
        # The tail arms the deadline at _now() + 10 == 1010.
        gen = _stream_events(cast(Request, _AliveRequest(alive=2)), wired.store, wired.settings, "0-0")
        tail = [frame async for frame in gen]
    # The filtered window emitted nothing and left the deadline at 1010; only window 2's
    # deadline crossing emits a keepalive. A dropped `_now() >= next_keepalive` guard
    # would instead fire a keepalive every non-yielding window, doubling the count. The
    # connect comment flushes first, before either tail window.
    assert tail == [_CONNECT_FRAME, ": keepalive\n\n"]


async def test_keepalive_deadline_rearmed_by_delivered_frame(wired):
    # A frame delivered to THIS caller re-arms the keepalive deadline, so an idle
    # window that follows soon after does NOT emit a keepalive. Two clock-controlled
    # tail windows, no real waits:
    #   window 1: a keyA-addressed (entitled) add lands BEFORE the armed deadline
    #             (clock 1000 -> 1008, deadline 1010). Delivering it re-arms the
    #             deadline to _now() + 10 == 1018.
    #   window 2: an idle window advances past the ORIGINAL deadline but not the
    #             re-armed one (clock 1008 -> 1012; 1010 < 1012 < 1018). No keepalive
    #             may fire — were the delivered frame not to re-arm the deadline, this
    #             window would spuriously emit one.
    # Asserting on the full frame list makes any spurious keepalive visible.
    clock = {"t": 1000.0}
    wired.monkeypatch.setattr(router, "_now", lambda: clock["t"])
    wired.monkeypatch.setattr(router, "_KEEPALIVE_SECONDS", 10)

    real_xread = wired.fake.xread
    advances = iter([8, 4])  # window 1 stays before the deadline; window 2 crosses only the original
    injected = {"done": False}

    async def _advancing_xread(streams, block=None):
        # A keyA-addressed add lands live on the first window (cursor captured an empty
        # tail): window 1's XREAD returns it and the entitled keyA caller delivers it.
        if not injected["done"]:
            injected["done"] = True
            await _seed_addressed(wired, "a1", "ga", "keyA")
        result = await real_xread(streams, block=block)
        clock["t"] += next(advances, 0)  # model time elapsed while blocked in this window
        return result

    wired.monkeypatch.setattr(wired.fake, "xread", _advancing_xread)

    with _identity(user_id="keyA", owner="alice"):
        # The tail arms the deadline at _now() + 10 == 1010.
        gen = _stream_events(cast(Request, _AliveRequest(alive=2)), wired.store, wired.settings, "0-0")
        tail = [frame async for frame in gen]
    # Only the entitled add rides the tail (after the leading connect comment); the
    # following idle window stays silent because delivering the add re-armed the deadline
    # to 1018. Dropping the `if yielded` reset would leave the deadline at 1010 and emit a
    # spurious keepalive.
    assert tail[0] == _CONNECT_FRAME
    assert len(tail) == 2, tail
    assert _add_ids(tail) == ["a1"]
    assert ": keepalive\n\n" not in tail


async def test_stream_tail_forwards_answered_and_removed(wired):
    # Events landing AFTER the cursor is captured (empty-at-capture -> "0-0") are
    # delivered by the tail: answered + removed frames both forward.
    async def _inject():
        wired.fake._xadd(
            wired.store.events_key, {"type": "interaction.answered", "interaction_id": "i9", "group_id": "g9"}
        )
        wired.fake._xadd(
            wired.store.events_key, {"type": "interaction.removed", "interaction_id": "i9", "group_id": "g9"}
        )

    frames = await _tail_collect(wired, _inject)
    assert any(_is_event(f, "interaction.answered") for f in frames)
    assert any(_is_event(f, "interaction.removed") for f in frames)


async def test_stream_tail_skips_malformed_event_without_crashing(wired):
    # A stream entry missing a required field (partial XADD / older-or-newer schema
    # / seeded frame) must be SKIPPED, not tear down the whole SSE tail: a valid
    # event queued behind it is still delivered.
    async def _inject():
        # A malformed frame (missing group_id) followed by a well-formed answered frame.
        wired.fake._xadd(wired.store.events_key, {"type": "interaction.answered", "interaction_id": "bad"})
        wired.fake._xadd(
            wired.store.events_key, {"type": "interaction.answered", "interaction_id": "i9", "group_id": "g9"}
        )

    frames = await _tail_collect(wired, _inject)
    answered = [f for f in frames if _is_event(f, "interaction.answered")]
    # The malformed entry was skipped; only the well-formed one surfaced.
    assert len(answered) == 1
    assert json.loads(_frame_fields(answered[0])["data"])["interaction_id"] == "i9"


async def test_stream_frames_carry_the_stream_id(wired):
    # Every event frame leads with an ``id:`` = the Redis stream message-id — the
    # resume token a reconnecting client echoes back as Last-Event-ID.
    async def _inject():
        await _seed(wired, iid="i1", gid="g1")

    frames = await _tail_collect(wired, _inject)
    add_frames = [f for f in frames if _is_event(f, "interaction.add")]
    assert add_frames, frames
    stream_id = _frame_fields(add_frames[0]).get("id")
    assert stream_id, add_frames[0]
    # It is the actual Redis message-id (a ``<ms>-<seq>`` token), not a placeholder.
    ms, _, seq = stream_id.partition("-")
    assert ms.isdigit(), stream_id
    assert seq.isdigit(), stream_id


async def test_stream_resume_delivers_gap_answered_frame(wired):
    # Composed path (the live report): a card goes pending, the client saw the add
    # frame's id, the connection drops, an ``answered`` event is XADDed WHILE the
    # client is disconnected, then the client reconnects sending that id as
    # Last-Event-ID. The resumed tail resumes AFTER the id and DELIVERS the gap
    # ``answered`` frame, so the card converges to answered instead of vanishing.
    from starlette.responses import StreamingResponse

    await _seed(wired, iid="i1", gid="g1")  # the add event -> the client's last-seen id
    last_seen = await _events_cursor(wired)
    # The answer lands during the disconnect gap.
    prior = InteractionResponse(
        interaction_id="i1", answer={}, answered_by="external-callback", answered_at=datetime.now(UTC)
    )
    await wired.store.record_answer(wired.fake, prior, "g1", reply_ttl=60, ticket="TKT", ticket_ttl=86400)
    # Reconnect carrying the last-seen id as Last-Event-ID.
    resp = await router.stream(cast(Request, _AliveRequest(alive=1, headers={"last-event-id": last_seen})))
    assert isinstance(resp, StreamingResponse)
    frames = [cast(str, f) async for f in resp.body_iterator]
    answered = [f for f in frames if _is_event(f, "interaction.answered")]
    assert answered, frames
    assert json.loads(_frame_fields(answered[0])["data"])["interaction_id"] == "i1"
    # The resumed frame still carries its own id (so the next reconnect resumes past it).
    assert _frame_fields(answered[0]).get("id")


async def test_stream_resume_falls_back_when_last_event_id_trimmed(wired, caplog):
    # A Last-Event-ID that predates the retained stream window (the id was trimmed)
    # must NOT silently resume past a gap it cannot cover: the route falls back to the
    # tail END (the client's pending-base reseed via the paged list door heals the
    # rest) and LOGS the fallback rather than silently skipping.
    from starlette.responses import StreamingResponse

    await _seed(wired, iid="i1", gid="g1")  # add event, well past a "0-1" id
    prior = InteractionResponse(
        interaction_id="i1", answer={}, answered_by="external-callback", answered_at=datetime.now(UTC)
    )
    await wired.store.record_answer(wired.fake, prior, "g1", reply_ttl=60, ticket="TKT", ticket_ttl=86400)
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.routers.interactions"):
        resp = await router.stream(cast(Request, _AliveRequest(alive=1, headers={"last-event-id": "0-1"})))
        assert isinstance(resp, StreamingResponse)
        frames = [cast(str, f) async for f in resp.body_iterator]
    # Fell back to the tail END: the gap answered frame is NOT replayed on the wire
    # (the client reseeds the answered/pending state from the list door instead).
    assert not [f for f in frames if _is_event(f, "interaction.answered")]
    # Never silent: the fallback is logged.
    assert any("trimmed" in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]


async def test_stream_resume_falls_back_when_last_event_id_malformed(wired, caplog):
    # A Last-Event-ID the client never got from us (it does not parse as a ``<ms>-<seq>``
    # stream id) is not a resume we can honour: the route falls back to the tail END (the
    # paged pending-base reseed heals the rest) and LOGS the malformed id rather than
    # silently skipping — the never-silent guarantee's ValueError branch.
    from starlette.responses import StreamingResponse

    await _seed(wired, iid="i1", gid="g1")  # add event
    prior = InteractionResponse(
        interaction_id="i1", answer={}, answered_by="external-callback", answered_at=datetime.now(UTC)
    )
    await wired.store.record_answer(wired.fake, prior, "g1", reply_ttl=60, ticket="TKT", ticket_ttl=86400)
    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.routers.interactions"):
        resp = await router.stream(cast(Request, _AliveRequest(alive=1, headers={"last-event-id": "not-an-id"})))
        assert isinstance(resp, StreamingResponse)
        frames = [cast(str, f) async for f in resp.body_iterator]
    # Fell back to the tail END: the gap answered frame is NOT replayed on the wire.
    assert not [f for f in frames if _is_event(f, "interaction.answered")]
    # Never silent: the malformed id is logged at WARNING.
    assert any("malformed" in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]


async def test_restricted_stream_terminal_filtered_by_event_audience(wired):
    # A terminal frame is filtered DIRECTLY on the audience the store stamps into the
    # event payload: a restricted caller sees a terminal ONLY for its own addressed
    # interaction; another identity's and an unaddressed one are suppressed.
    async def _inject():
        ev = wired.store.events_key
        wired.fake._xadd(
            ev, {"type": "interaction.answered", "interaction_id": "mine", "group_id": "gm", "audience": "keyA"}
        )
        wired.fake._xadd(
            ev, {"type": "interaction.answered", "interaction_id": "bob", "group_id": "gb", "audience": "bob"}
        )
        wired.fake._xadd(ev, {"type": "interaction.removed", "interaction_id": "cast", "group_id": "gc"})  # unaddressed
        wired.fake._xadd(
            ev, {"type": "interaction.removed", "interaction_id": "mine2", "group_id": "gm2", "audience": "keyA"}
        )

    frames = await _tail_collect(wired, _inject, identity=_identity(user_id="keyA", owner="alice"))
    assert _event_ids(frames, "interaction.answered") == ["mine"]
    assert _event_ids(frames, "interaction.removed") == ["mine2"]


async def test_unrestricted_stream_terminal_sees_all_audiences(wired):
    # An unrestricted operator receives every terminal frame regardless of its audience.
    async def _inject():
        ev = wired.store.events_key
        wired.fake._xadd(
            ev, {"type": "interaction.answered", "interaction_id": "a1", "group_id": "ga", "audience": "keyA"}
        )
        wired.fake._xadd(ev, {"type": "interaction.answered", "interaction_id": "o1", "group_id": "go"})

    frames = await _tail_collect(wired, _inject, identity=_identity(user_id="op1", owner=None))
    assert sorted(_event_ids(frames, "interaction.answered")) == ["a1", "o1"]


async def test_restricted_stream_live_add_other_identity_not_emitted(wired):
    # A live ADD (arriving after cursor capture) addressed to bob must NOT reach the
    # restricted keyA caller — the live-add path filters on the record's audience.
    async def _inject():
        await _seed_addressed(wired, "b1", "gb", "bob")

    frames = await _tail_collect(wired, _inject, identity=_identity(user_id="keyA", owner="alice"))
    assert _add_ids(frames) == []


async def test_restricted_stream_live_add_own_identity_emitted_and_terminal_delivered(wired):
    # A live ADD addressed to keyA IS emitted, and its subsequent answered frame — which
    # carries audience keyA in the event payload — is delivered to the keyA caller.
    async def _inject():
        await _seed_addressed(wired, "a1", "ga", "keyA")
        await wired.store.record_answer(
            wired.fake,
            InteractionResponse(interaction_id="a1", answer="x", answered_by="t", answered_at=datetime.now(UTC)),
            "ga",
            reply_ttl=60,
        )

    frames = await _tail_collect(wired, _inject, identity=_identity(user_id="keyA", owner="alice"))
    assert _add_ids(frames) == ["a1"]
    assert _event_ids(frames, "interaction.answered") == ["a1"]


async def test_stream_add_frame_carries_no_ticket_field(wired):
    # The ticket-containment invariant: no add frame carries a ticket for ANY caller,
    # so a restricted caller consuming the stream can never obtain one — the callback
    # door (channel-only ticket delivery) stays the sole ticket-bearing surface.
    assert "ticket" not in _add_data(_external_request(wired.store))
    assert "ticket" not in _add_data(_plain_request(wired.store, AnswerFormat.TEXT, audience="alice"))


async def test_stream_off_when_store_unconfigured_returns_501(wired):
    # The stream door answers a plain 501+code up front rather than opening a 200 SSE
    # body that dies mid-stream reaching for an absent Redis — never a StreamingResponse.
    from starlette.responses import JSONResponse, StreamingResponse

    wired.monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    wired.monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    resp = await router.stream(make_request("GET"))
    assert isinstance(resp, JSONResponse)
    assert not isinstance(resp, StreamingResponse)
    assert resp.status_code == 501
    body = json.loads(bytes(resp.body))
    assert body["code"] == "interactions-not-configured"
    assert "error" in body
