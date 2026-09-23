"""Shared builders, fakes and request harness for the interactions router tests."""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

from starlette.requests import Request
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id
from tai42_contract.interactions import AnswerFormat, InteractionRequest, InteractionResponse, MediaItem, MediaKind

from tai42_skeleton.access_control.request_scopes import reset_request_identity_claims, set_request_identity_claims
from tai42_skeleton.routers.interactions.stream import _stream_events


def make_request(method, *, path_params=None, query="", body=b"", headers=None, client=("1.2.3.4", 1111)):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/interactions/callback/x",
        "query_string": query.encode(),
        "headers": hdrs,
        "client": client,
        "path_params": path_params or {},
    }
    delivered = {"done": False}

    async def receive():
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _external_request(store, iid="i1", gid="g1", schema=None, budget=60, verifier=None) -> InteractionRequest:
    now = datetime.now(UTC)
    payload = {"url": "https://ext.example/resource"}
    if schema is not None:
        payload["schema"] = schema
    if verifier is not None:
        payload["verifier"] = verifier
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="Sign?",
        answer_format=AnswerFormat.EXTERNAL,
        format_payload=payload,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(seconds=budget),
    )


async def _seed(w, *, ticket="TKT", schema=None, budget=60, iid="i1", gid="g1", verifier=None) -> str:
    request = _external_request(w.store, iid, gid, schema, budget, verifier)
    await w.store.add(w.fake, request, idle_ttl=86400, ticket=ticket, ticket_ttl=budget)
    return iid


async def _seed_form(w, *, schema, ticket="TKT", iid="i1", gid="g1", budget=60) -> str:
    now = datetime.now(UTC)
    request = InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="Fill?",
        answer_format=AnswerFormat.FORM,
        format_payload={"schema": schema},
        reply_to=w.store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(seconds=budget),
    )
    await w.store.add(w.fake, request, idle_ttl=86400, ticket=ticket, ticket_ttl=budget)
    return iid


def _json(resp) -> dict:
    return json.loads(bytes(resp.body))


async def _seed_form_payload(w, *, format_payload, ticket="TKT", iid="i1", gid="g1", budget=60) -> str:
    now = datetime.now(UTC)
    request = InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="Fill?",
        answer_format=AnswerFormat.FORM,
        format_payload=format_payload,
        reply_to=w.store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(seconds=budget),
    )
    await w.store.add(w.fake, request, idle_ttl=86400, ticket=ticket, ticket_ttl=budget)
    return iid


def _caller_ask_request(store, *, iid="c1", gid="cg", fmt=AnswerFormat.TEXT, payload=None, budget=3600):
    """A ``to="caller"`` ask: async, addressed to the calling run (never a person)."""
    now = datetime.now(UTC)
    future = now + timedelta(seconds=budget)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="proceed?",
        answer_format=fmt,
        format_payload=payload,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=future,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=future,
        to="caller",
    )


def _plain_request(store, fmt, iid="p1", gid="pg", payload=None, audience=None) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="?",
        answer_format=fmt,
        format_payload=payload,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
        audience=audience,
    )


@contextmanager
def _identity(*, user_id: str | None = None, owner: str | None = None) -> Iterator[None]:
    """Bind a caller identity for a door/stream call: ``owner`` set makes it a
    RESTRICTED owned key (isolated to its OWN id ``user_id`` — NOT its owner; each key
    is its own island), ``owner=None`` an unrestricted caller. Tests pass an ``owner``
    DIFFERENT from ``user_id`` so the key-own-vs-owner distinction is exercised."""
    claims: dict[str, str] = {} if owner is None else {OWNER_USER_ID_CLAIM: owner}
    uid_token = set_request_user_id(user_id) if user_id is not None else None
    claims_token = set_request_identity_claims(claims)
    try:
        yield
    finally:
        reset_request_identity_claims(claims_token)
        if uid_token is not None:
            reset_request_user_id(uid_token)


async def _collect_stream(gen) -> list[str]:
    frames = []
    async for frame in gen:
        frames.append(frame)
    return frames


def _frame_fields(frame: str) -> dict[str, str]:
    # Parse one SSE frame into its ``id``/``event``/``data`` fields. Every event
    # frame now leads with an ``id:`` line (the Redis stream message-id for the
    # Last-Event-ID resume), so tests match on the parsed ``event`` field rather
    # than a frame prefix. Comment frames (``:`` lead) carry no fields.
    fields: dict[str, str] = {}
    for line in frame.split("\n"):
        if not line or line.startswith(":"):
            continue
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


def _is_event(frame: str, event: str) -> bool:
    return _frame_fields(frame).get("event") == event


async def _events_cursor(wired) -> str:
    """The events-stream tail cursor the route handler captures BEFORE returning the
    response — reproduced here so the generator is driven exactly as production drives
    it (an empty stream yields ``0-0``)."""
    tail = await wired.fake.xrevrange(wired.store.events_key, count=1)
    return tail[0][0] if tail else "0-0"


async def _tail_collect(wired, inject, *, alive: int = 1, identity=None) -> list[str]:
    """Drive the TAIL-ONLY stream and return its frames. The cursor is captured on an
    empty events tail (``0-0``); ``inject`` (an async callback) runs on the first XREAD
    — BEFORE the tail reads — so the events it writes ride that read as unambiguous live
    tail frames (the stream carries no backlog). ``alive`` bounds the connected windows;
    ``identity`` is an optional ``_identity(...)`` context for a restricted caller."""
    real_xread = wired.fake.xread
    injected = {"done": False}

    async def _hooked_xread(streams, block=None):
        if not injected["done"]:
            injected["done"] = True
            await inject()
        return await real_xread(streams, block=block)

    wired.monkeypatch.setattr(wired.fake, "xread", _hooked_xread)
    ctx = identity if identity is not None else contextlib.nullcontext()
    cursor = await _events_cursor(wired)
    with ctx:
        gen = _stream_events(cast(Request, _AliveRequest(alive=alive)), wired.store, wired.settings, cursor)
        return [frame async for frame in gen]


def _stream_request() -> Request:
    """A GET request that yields one empty receive then disconnects — the tail-driven
    stream harness (one live-tail iteration, then the disconnect ends it)."""
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

    return Request(scope, receive)


class _AliveRequest:
    """A request that reports connected for exactly ``alive`` tail iterations, then
    disconnects — lets a clock-driven test step the tail a fixed number of windows."""

    def __init__(self, alive: int, headers: dict[str, str] | None = None) -> None:
        self._alive = alive
        # ``stream()`` reads the Last-Event-ID header + query param on connect;
        # empty mappings stand in for the plain (no-resume) connect these tests drive.
        self.headers: dict[str, str] = headers or {}
        self.query_params: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        if self._alive > 0:
            self._alive -= 1
            return False
        return True


def _add_ids(frames: list[str]) -> list[str]:
    return [json.loads(_frame_fields(f)["data"])["interaction_id"] for f in frames if _is_event(f, "interaction.add")]


def _event_ids(frames: list[str], event: str) -> list[str]:
    return [json.loads(_frame_fields(f)["data"])["interaction_id"] for f in frames if _is_event(f, event)]


def _plain_request_dated(store, iid, gid, created, timeout) -> InteractionRequest:
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=created,
        timeout_at=timeout,
    )


def _sensitive_request(store, iid="s1", gid="sg", budget=60) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="Paste your API key",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(seconds=budget),
        sensitive=True,
    )


_MEDIA_ITEMS: list[MediaItem] = [
    MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/p.png", caption="A product"),
    MediaItem(kind=MediaKind.LINK, url="https://docs.example/p"),
]
# The wire form is exclude_none: the caption-less link carries no ``caption`` key.


_EXPECTED_MEDIA_FRAME = [
    {"kind": "image", "url": "https://cdn.example/p.png", "caption": "A product"},
    {"kind": "link", "url": "https://docs.example/p"},
]


def _store_empty(fake_redis) -> bool:
    # Every FakeRedis store empty — nothing was written (``_lists`` is the reply channel,
    # ``_sets`` the media index).
    return not (
        fake_redis._hashes
        or fake_redis._streams
        or fake_redis._zsets
        or fake_redis._strings
        or fake_redis._lists
        or fake_redis._sets
    )


def _media_request(
    store, iid="m1", gid="mg", media: list[MediaItem] | None = None, audience=None, budget=60
) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="Pick a product",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(seconds=budget),
        media=media,
        audience=audience,
    )


async def _tail_add_frames(wired, request: InteractionRequest) -> list[dict]:
    """Drive the live-tail path for ``request`` in isolation: the cursor captures an
    empty tail and ``request`` is added only on the first XREAD — so the add frame can
    come ONLY from the live tail (the stream carries no backlog). Returns the parsed
    add payloads."""

    async def _inject():
        await wired.store.add(wired.fake, request, idle_ttl=86400)

    frames = await _tail_collect(wired, _inject)
    return [json.loads(_frame_fields(f)["data"]) for f in frames if _is_event(f, "interaction.add")]


_PNG_BYTES = bytes.fromhex("89504e470d0a1a0a")


_DATA_PNG = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()


async def _store_media(wired, media_id: str, ttl: int = 120) -> None:
    # Write the media hash directly (the ``wired`` fixture pins ``secrets.token_urlsafe``,
    # so ``store_data_image``'s random id is stubbed; the route reads by a known id here).
    key = wired.store.media_key(media_id)
    wired.fake._hset(key, mapping={"mime": "image/png", "b64": base64.b64encode(_PNG_BYTES).decode()})
    wired.fake._expire(key, ttl)


async def _seed_addressed(wired, iid, gid, audience) -> None:
    await wired.store.add(
        wired.fake, _plain_request(wired.store, AnswerFormat.TEXT, iid=iid, gid=gid, audience=audience), idle_ttl=86400
    )


class _FakeVerifier:
    """Records each verify call; optionally raises. Order-tracks against a shared
    list so a test can pin verify-before-record."""

    post_only = True

    def __init__(self, *, raise_exc=None, order=None):
        self._raise = raise_exc
        self._order = order
        self.calls: list = []

    async def verify(self, body, headers, config):
        if self._order is not None:
            self._order.append("verify")
        self.calls.append((body, dict(headers), config))
        if self._raise is not None:
            raise self._raise

    def replay_defense(self, body, headers, config):
        # The callback door is replay-safe by its single-use ticket + atomic answer
        # claim, so it never consults this; present only to satisfy the verifier contract.
        from tai42_contract.webhooks import FreshnessWindow

        return FreshnessWindow()


_BINDING = {"name": "prov", "config": {"secret_env": "WH"}}


async def _answer_bound(wired, *, answer=None, ticket="TKT", iid="i1", gid="g1") -> None:
    """Drive a seeded bound question into the answered state, preserving its
    verifier binding and refreshing the ticket TTL exactly as the callback door
    does — so a later GET/POST sees a bound + answered ticket."""
    prior = InteractionResponse(
        interaction_id=iid,
        answer={"x": 1} if answer is None else answer,
        answered_by="external-callback",
        answered_at=datetime.now(UTC),
    )
    await wired.store.record_answer(wired.fake, prior, gid, reply_ttl=60, ticket=ticket, ticket_ttl=86400)


def _typed_request(store, fmt, iid="t1", gid="tg1", options=None, budget=60, channel="chan") -> InteractionRequest:
    now = datetime.now(UTC)
    payload = {"options": options} if options is not None else None
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="Q?",
        answer_format=fmt,
        format_payload=payload,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(seconds=budget),
        channel=channel,
    )


async def _seed_typed(w, fmt, *, options=None, ticket="TKT") -> str:
    request = _typed_request(w.store, fmt, options=options)
    await w.store.add(w.fake, request, idle_ttl=86400, ticket=ticket, ticket_ttl=60)
    return "t1"


def _off(wired) -> None:
    wired.monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    wired.monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)


def _async_park(store, *, iid="p1", gid="pg", channel=None, recipient=None, audience=None) -> InteractionRequest:
    now = datetime.now(UTC)
    future = now + timedelta(hours=1)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=future,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=future,
        channel=channel,
        recipient=recipient,
        audience=audience,
    )
