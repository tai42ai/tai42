"""The fake-channel end-to-end loop, offline: ``ask_user(channel=...)``
delivers through a registered channel, the recorded ``callback_url`` receives
the human's reply as ``{"answer": <value>}`` through the public callback door
(which validates it against the stored format), and the blocked caller returns
the typed value — deliver -> inbound bridge -> callback claim -> resume, proven
with no network and no external stack.

Handlers are driven directly (the router-test pattern); Redis is the shared
in-memory fake, wired at both the router's and the helper's ``client_ctx``
seams so the blocked ``ask_user`` caller and the callback door share one store.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelInputError
from tai42_contract.interactions import (
    MediaItem,
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)

from tai42_skeleton.app.instance import app
from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import InteractionStore, InteractionTimeoutError, ask_user
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.routers import interactions as router

from .._helpers import DeliverOnlyChannel, await_add_event


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # the interactions surface is OFF with no Redis. These tests exercise the ON
    # feature, so configure its store — the fake connection still stands in; only the
    # presence gate reads this env var.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


class FakeChannel(DeliverOnlyChannel):
    """Records every delivery; the test then plays the plugin's part by POSTing
    the human's typed answer to the recorded ``callback_url``."""

    def __init__(self) -> None:
        self.deliveries: list[ChannelDelivery] = []

    async def deliver(self, delivery: ChannelDelivery) -> None:
        self.deliveries.append(delivery)


class FailingChannel(DeliverOnlyChannel):
    async def deliver(self, delivery: ChannelDelivery) -> None:
        raise ChannelDeliveryError("provider unreachable")


class BuggyChannel(DeliverOnlyChannel):
    async def deliver(self, delivery: ChannelDelivery) -> None:
        raise RuntimeError("plugin bug")


class FormChannel(FakeChannel):
    """A channel that advertises the ``supports_form_delivery`` capability, so a
    ``form`` question is delivered to it (schema carried on the delivery)."""

    supports_form_delivery = True


class RecordingHooks:
    """Captures every ``on_event`` the helper emits, to prove the delivery-failed
    platform event fires (or does not) on the terminal-abandonment path."""

    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    async def on_event(self, topic, payload, *, tool_kwargs_override=None) -> None:
        self.events.append(SimpleNamespace(topic=topic, payload=payload))


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    settings = InteractionsSettings(public_base_url="https://cb.example")
    monkeypatch.setattr(router, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(router, "interactions_settings", lambda: settings)
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    # The detached continuation fire clears its durable due-record through the
    # continuation module's own client seam — point it at the same fake.
    monkeypatch.setattr(continuation_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(continuation_module, "interactions_settings", lambda: settings)
    store = InteractionStore(settings.key_prefix)
    return SimpleNamespace(settings=settings, store=store, fake=fake_redis)


@pytest.fixture
def fake_channel():
    channel = FakeChannel()
    app._channel_registry.reset()
    tai42_app.channels.register("fake", channel)
    yield channel
    app._channel_registry.reset()


def _make_request(method, *, path_params=None, query="", body=b"", headers=None):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/interactions/callback/x",
        "query_string": query.encode(),
        "headers": hdrs,
        "client": ("1.2.3.4", 1111),
        "path_params": path_params or {},
    }
    delivered = {"done": False}

    async def receive():
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def _await_delivery(channel: FakeChannel, timeout: float = 2.0) -> ChannelDelivery:
    """Poll until ``deliver`` ran (it runs after the add event, inside the asking
    task) — fail the test on timeout, never hang."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if channel.deliveries:
            return channel.deliveries[0]
        await asyncio.sleep(0.01)
    raise AssertionError("channel.deliver was never called")


def _ticket(delivery: ChannelDelivery) -> str:
    return delivery.callback_url.rsplit("/", 1)[-1]


def _tune(monkeypatch, wired, **overrides) -> InteractionsSettings:
    """Re-point both seams at a copy of the wired settings carrying ``overrides``
    (the retry knobs, dialled down so a test never waits whole seconds)."""
    settings = wired.settings.model_copy(update=overrides)
    monkeypatch.setattr(router, "interactions_settings", lambda: settings)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    return settings


def _empty(fake_redis) -> bool:
    # All five FakeRedis stores, ``_lists`` included (the reply channel is a list).
    return not (
        fake_redis._hashes or fake_redis._streams or fake_redis._zsets or fake_redis._strings or fake_redis._lists
    )


# -- the loop ------------------------------------------------------------------


async def test_fake_channel_text_loop(wired, fake_channel):
    # 1. an agent-side ask blocks, bound to the registered channel
    task = asyncio.create_task(ask_user("What is the magic word?", channel="fake", timeout=5))
    iid, _gid = await await_add_event(wired.fake, wired.store)

    # 2. deliver() ran exactly once with the full delivery contract
    delivery = await _await_delivery(fake_channel)
    assert len(fake_channel.deliveries) == 1
    assert delivery.interaction_id == iid
    assert delivery.question == "What is the magic word?"
    assert delivery.answer_format == "text"
    assert delivery.options is None
    assert delivery.recipient is None  # no caller address -> the plugin's default
    assert delivery.callback_url.startswith("https://cb.example/api/interactions/callback/")

    # 3. the persisted request carries the channel; the add frame emits it
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.request.channel == "fake"
    assert router._add_data(state.request)["channel"] == "fake"

    # 4. play the plugin: forward the human's reply as {"answer": <value>}
    resp = await router.callback(
        _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "please"}')
    )
    assert resp.status_code == 200
    assert json.loads(bytes(resp.body))["data"]["status"] == "answered"

    # 5. the blocked ask_user returns the TYPED answer (str) — loop closed
    assert await task == "please"


async def test_fake_channel_confirm_loop(wired, fake_channel):
    task = asyncio.create_task(ask_user("Deploy?", answer_format="confirm", channel="fake", timeout=5))
    await await_add_event(wired.fake, wired.store)
    delivery = await _await_delivery(fake_channel)
    assert delivery.answer_format == "confirm"

    # Tap flow: the GET-confirm page's form POSTs an empty body → recorded True.
    resp = await router.callback(_make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b""))
    assert resp.status_code == 200
    assert await task is True  # the TYPED bool


async def test_add_frame_omits_channel_when_unset(wired):
    # No channel → the add frame has NO ``channel`` key (conditional, like
    # ``server_verified``) and the persisted request carries None.
    task = asyncio.create_task(ask_user("q", timeout=5))
    iid, _gid = await await_add_event(wired.fake, wired.store)
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.request.channel is None
    assert "channel" not in router._add_data(state.request)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_delivery_runs_after_persist(wired, fake_channel):
    # The callback ticket must be claimable at deliver time: the persisted state
    # already exists when the channel is handed the delivery.
    seen: dict = {}

    class ProbingChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            seen["state"] = await wired.store.get_state(wired.fake, delivery.interaction_id)
            seen["delivery"] = delivery

    app._channel_registry.reset()
    tai42_app.channels.register("probe", ProbingChannel())
    try:
        task = asyncio.create_task(ask_user("q", channel="probe", timeout=5))
        _iid, _gid = await await_add_event(wired.fake, wired.store)
        deadline = asyncio.get_event_loop().time() + 2.0
        while "delivery" not in seen and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)

        assert seen["state"] is not None  # persisted BEFORE deliver
        resp = await router.callback(
            _make_request("POST", path_params={"ticket": _ticket(seen["delivery"])}, body=b'{"answer": "done"}')
        )
        assert resp.status_code == 200
        assert await task == "done"
    finally:
        app._channel_registry.reset()


async def test_channel_external_url_is_callback_url(wired, fake_channel):
    # For a channel-delivered external ask, the stored tappable url IS the
    # callback door — no link builder involved.
    task = asyncio.create_task(ask_user("Approve?", answer_format="external", channel="fake", timeout=5))
    iid, _gid = await await_add_event(wired.fake, wired.store)
    delivery = await _await_delivery(fake_channel)

    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.request.format_payload is not None
    assert state.request.format_payload["url"] == delivery.callback_url

    # EXTERNAL keeps the verbatim-dict semantics — no {"answer": ...} envelope.
    resp = await router.callback(
        _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"approved": true}')
    )
    assert resp.status_code == 200
    assert await task == {"approved": True}


async def test_recipient_forwarded_in_delivery(wired, fake_channel):
    # The caller-requested address rides the delivery verbatim; the skeleton
    # resolves nothing — allowlist validation is the plugin's job.
    task = asyncio.create_task(ask_user("Ping?", channel="fake", recipient="123456", timeout=5))
    await await_add_event(wired.fake, wired.store)
    delivery = await _await_delivery(fake_channel)

    assert delivery.recipient == "123456"

    resp = await router.callback(
        _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "pong"}')
    )
    assert resp.status_code == 200
    assert await task == "pong"


async def test_recipient_persisted_on_record(wired, fake_channel):
    # The caller-requested address is also recorded on the durable question (feed
    # attribution), not only forwarded to the channel delivery.
    task = asyncio.create_task(ask_user("Ping?", channel="fake", recipient="123456", timeout=5))
    iid, _gid = await await_add_event(wired.fake, wired.store)
    delivery = await _await_delivery(fake_channel)

    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.request.recipient == "123456"

    resp = await router.callback(
        _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "pong"}')
    )
    assert resp.status_code == 200
    assert await task == "pong"


async def test_channel_delivery_omits_media_inbox_keeps_it(wired, fake_channel):
    # Display-only media is NOT forwarded to a channel: the ChannelDelivery handed
    # to the plugin carries no media, while the persisted question keeps it and the
    # inbox add frame renders it. A documented limit, never a silent drop.
    media: list[MediaItem | dict[str, Any]] = [
        {"kind": "image", "url": "https://cdn.example/p.png", "caption": "A product"}
    ]
    task = asyncio.create_task(ask_user("Which?", channel="fake", media=media, timeout=5))
    iid, _gid = await await_add_event(wired.fake, wired.store)
    delivery = await _await_delivery(fake_channel)

    assert not hasattr(delivery, "media")  # the delivery contract carries no media

    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert router._add_data(state.request)["media"] == media

    resp = await router.callback(
        _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "ok"}')
    )
    assert resp.status_code == 200
    assert await task == "ok"


async def test_channel_select_delivery_carries_options(wired, fake_channel):
    task = asyncio.create_task(
        ask_user("Pick one", answer_format="select", options=["red", "blue"], channel="fake", timeout=5)
    )
    await await_add_event(wired.fake, wired.store)
    delivery = await _await_delivery(fake_channel)

    assert delivery.answer_format == "select"
    assert delivery.options == ["red", "blue"]

    resp = await router.callback(
        _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "blue"}')
    )
    assert resp.status_code == 200
    assert await task == "blue"  # the TYPED chosen option, not a dict


# -- loud validation (nothing persisted) ----------------------------------------


async def test_unknown_channel_rejected_up_front(wired):
    app._channel_registry.reset()
    with pytest.raises(ValueError, match="unknown channel"):
        await ask_user("q", channel="nope", timeout=5)
    assert _empty(wired.fake)  # rejected BEFORE any state was written


async def test_channel_forbids_link_non_external(wired, fake_channel):
    with pytest.raises(ValueError, match="link is forbidden when a channel is set"):
        await ask_user("q", channel="fake", link="{callback_url}", timeout=5)
    assert _empty(wired.fake)


async def test_channel_forbids_link_external(wired, fake_channel):
    with pytest.raises(ValueError, match="link is forbidden when a channel is set"):
        await ask_user("q", answer_format="external", channel="fake", link="{callback_url}", timeout=5)
    assert _empty(wired.fake)


async def test_channel_forbids_verifier(wired, fake_channel):
    # A channel's forward is unsigned — a bound verifier would 401 every reply.
    with pytest.raises(ValueError, match="verifier is forbidden when a channel is set"):
        await ask_user(
            "q", answer_format="external", channel="fake", verifier={"name": "github", "config": {}}, timeout=5
        )
    assert _empty(wired.fake)


async def test_channel_without_form_capability_rejects_form(wired, fake_channel):
    # FakeChannel does not advertise ``supports_form_delivery``: a form is refused
    # loudly, naming the channel — nothing persisted.
    with pytest.raises(ValueError, match="channel 'fake' does not deliver form questions"):
        await ask_user(
            "q", answer_format="form", schema={"type": "object", "properties": {}}, channel="fake", timeout=5
        )
    assert _empty(wired.fake)


async def test_form_channel_without_schema_raises_before_persist(wired):
    # An advertising channel still requires a schema for a form; the missing-schema
    # guard fires in ``_build_payload`` BEFORE any state is written.
    app._channel_registry.reset()
    tai42_app.channels.register("form", FormChannel())
    try:
        with pytest.raises(ValueError, match="answer_format 'form' requires a schema"):
            await ask_user("q", answer_format="form", channel="form", timeout=5)
        assert _empty(wired.fake)
    finally:
        app._channel_registry.reset()


async def test_form_channel_delivery_carries_schema_and_loops(wired):
    # A form over a channel that advertises the capability: the delivery carries the
    # normalized schema, and the human's typed dict answer round-trips through the
    # callback door back to the blocked caller.
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    app._channel_registry.reset()
    channel = FormChannel()
    tai42_app.channels.register("form", channel)
    try:
        task = asyncio.create_task(
            ask_user("Fill this in", answer_format="form", schema=schema, channel="form", timeout=5)
        )
        iid, _gid = await await_add_event(wired.fake, wired.store)
        delivery = await _await_delivery(channel)
        assert delivery.answer_format == "form"
        assert delivery.schema == schema

        state = await wired.store.get_state(wired.fake, iid)
        assert state is not None
        assert state.request.format_payload == {"schema": schema}

        resp = await router.callback(
            _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": {"x": 7}}')
        )
        assert resp.status_code == 200
        assert await task == {"x": 7}
    finally:
        app._channel_registry.reset()


class _OptForm(BaseModel):
    # A nullable pydantic field -> ``anyOf`` (no scalar ``type``): outside the
    # channel-deliverable subset, so its JSON schema must be refused at ask time.
    name: str | None = None


@pytest.mark.parametrize(
    ("schema", "match"),
    [
        (_OptForm, "property 'name' has type None"),
        ({"type": "object", "properties": {"tags": {"type": "array"}}}, "property 'tags' has type 'array'"),
        (
            {"type": "object", "properties": {"n": {"type": "integer", "enum": [1, 2]}}},
            "property 'n' has an enum but is not a 'string'",
        ),
        ({"type": "object", "properties": {}}, "non-empty object 'properties'"),
        (
            {"type": "object", "required": ["ghost"], "properties": {"x": {"type": "string"}}},
            "'required' names undeclared properties",
        ),
    ],
)
async def test_channel_form_bad_schema_refused_before_persist(wired, schema, match):
    # A channel form whose schema is outside the renderable subset is refused
    # loudly at ask time — BEFORE any state is written (never a persisted question
    # whose callback form page would 500 on the human).
    app._channel_registry.reset()
    tai42_app.channels.register("form", FormChannel())
    try:
        with pytest.raises(ValueError, match=match):
            await ask_user("q", answer_format="form", schema=schema, channel="form", timeout=5)
        assert _empty(wired.fake)
    finally:
        app._channel_registry.reset()


class HookFormChannel(DeliverOnlyChannel):
    """A form channel with a channel-SPECIFIC schema limit — the property name
    ``reserved`` is unrenderable on this medium. The generic subset accepts it (a
    scalar string), so only the channel knows it is forbidden: the ask-time
    ``validate_form_schema`` hook refuses it (``ValueError``) exactly as the
    delivery path would (``ChannelInputError``)."""

    supports_form_delivery = True

    def __init__(self) -> None:
        self.deliveries: list[ChannelDelivery] = []

    def validate_form_schema(self, schema: dict[str, Any], question: str) -> None:
        if "reserved" in (schema.get("properties") or {}):
            raise ValueError("form property 'reserved': reserved on this channel")

    async def deliver(self, delivery: ChannelDelivery) -> None:
        self.deliveries.append(delivery)
        if "reserved" in ((delivery.schema or {}).get("properties") or {}):
            raise ChannelInputError("form property 'reserved': reserved on this channel")


class NoHookFormChannel(DeliverOnlyChannel):
    """The same medium WITHOUT the ask-time hook: only the delivery path refuses
    the reserved property, so a bad schema passes ask-time, persists, and fails
    only at delivery — the class of bug the hook closes."""

    supports_form_delivery = True

    def __init__(self) -> None:
        self.deliveries: list[ChannelDelivery] = []

    async def deliver(self, delivery: ChannelDelivery) -> None:
        self.deliveries.append(delivery)
        if "reserved" in ((delivery.schema or {}).get("properties") or {}):
            raise ChannelInputError("form property 'reserved': reserved on this channel")


_RESERVED_SCHEMA = {"type": "object", "properties": {"reserved": {"type": "string"}}}


async def test_validate_form_schema_hook_refuses_at_ask_time_before_persist(wired):
    # WITH the hook: the channel-specific limit is enforced at the chokepoint,
    # BEFORE any state is written — a ValueError naming the property, deliver never
    # reached, nothing persisted.
    app._channel_registry.reset()
    channel = HookFormChannel()
    tai42_app.channels.register("capform", channel)
    try:
        with pytest.raises(ValueError, match="'reserved'"):
            await ask_user("q", answer_format="form", schema=_RESERVED_SCHEMA, channel="capform", timeout=5)
        assert _empty(wired.fake)  # nothing persisted
        assert channel.deliveries == []  # delivery was never attempted
    finally:
        app._channel_registry.reset()


async def test_without_the_hook_the_bad_schema_persists_then_fails_at_delivery(wired):
    # The red baseline the hook fixes: a channel with NO ask-time hook passes the
    # generic subset check, PERSISTS the question, and only fails when delivery
    # refuses the reserved property — after which the state is pruned.
    app._channel_registry.reset()
    channel = NoHookFormChannel()
    tai42_app.channels.register("nohookform", channel)
    try:
        with pytest.raises(ChannelInputError, match="'reserved'"):
            await ask_user("q", answer_format="form", schema=_RESERVED_SCHEMA, channel="nohookform", timeout=5)
        assert channel.deliveries != []  # it reached delivery (persisted first)
        assert await wired.store.count_open(wired.fake) == 0  # pruned on the delivery failure
    finally:
        app._channel_registry.reset()


async def test_recipient_without_channel_rejected(wired):
    # An address is meaningless without a channel to send on.
    with pytest.raises(ValueError, match="recipient requires a channel"):
        await ask_user("q", recipient="123456", timeout=5)
    assert _empty(wired.fake)


@pytest.mark.parametrize("bad_recipient", ["", "   "])
async def test_blank_recipient_with_channel_rejected(wired, fake_channel, bad_recipient):
    # Rejected up-front as a clean ValueError — never a post-persist pydantic
    # error from the delivery frame's recipient validator.
    with pytest.raises(ValueError, match="recipient must be a non-empty address"):
        await ask_user("q", channel="fake", recipient=bad_recipient, timeout=5)
    assert _empty(wired.fake)


async def test_channel_rejects_non_select_options(wired, fake_channel):
    # Rejected up-front as a clean ValueError — never a post-persist pydantic error.
    with pytest.raises(ValueError, match="options are only valid with answer_format 'select'"):
        await ask_user("q", channel="fake", options=["a", "b"], timeout=5)
    assert _empty(wired.fake)


async def test_channel_requires_public_base_url(monkeypatch, fake_redis, fake_client_ctx, fake_channel):
    settings = InteractionsSettings(public_base_url=None)
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="INTERACTIONS_PUBLIC_BASE_URL"):
        await ask_user("q", channel="fake", timeout=5)


# -- failed delivery: prune + raise, never a silent zombie question -------------


async def test_delivery_failure_prunes_and_raises(wired):
    app._channel_registry.reset()
    tai42_app.channels.register("boom", FailingChannel())
    try:
        with pytest.raises(ChannelDeliveryError, match="provider unreachable"):
            await ask_user("q", channel="boom", timeout=5)

        # The persisted question was pruned: nothing open, nothing claimable.
        assert await wired.store.count_open(wired.fake) == 0
    finally:
        app._channel_registry.reset()


async def test_delivery_bug_prunes_and_raises(wired):
    app._channel_registry.reset()
    tai42_app.channels.register("buggy", BuggyChannel())
    try:
        with pytest.raises(RuntimeError, match="plugin bug"):
            await ask_user("q", channel="buggy", timeout=5)

        assert await wired.store.count_open(wired.fake) == 0
    finally:
        app._channel_registry.reset()


# -- delivery_failed platform event on terminal abandonment ---------------------


async def test_terminal_delivery_failure_emits_delivery_failed_once(wired, monkeypatch):
    # A send that fails for good (question pruned, nothing answered) STATES the fact as
    # exactly one ``interactions.delivery_failed`` event carrying the channel, the
    # interaction id, the recipient, and the error text — and the original delivery
    # error still propagates unchanged (the event rides alongside, never replaces it).
    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    captured: dict[str, str] = {}

    class CapturingFailer(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            captured["iid"] = delivery.interaction_id
            raise ChannelDeliveryError("provider unreachable")

    app._channel_registry.reset()
    tai42_app.channels.register("boom", CapturingFailer())
    try:
        with pytest.raises(ChannelDeliveryError, match="provider unreachable"):
            await ask_user("q", channel="boom", recipient="@ops", timeout=5)
    finally:
        app._channel_registry.reset()

    assert len(hooks.events) == 1
    event = hooks.events[0]
    assert event.topic == helper_module.DELIVERY_FAILED_EVENT_TOPIC == "interactions.delivery_failed"
    assert event.payload == {
        "channel": "boom",
        "interaction_id": captured["iid"],
        "recipient": "@ops",
        "error": "provider unreachable",
    }
    # The question was still pruned — the event does not change the terminal cleanup.
    assert await wired.store.count_open(wired.fake) == 0


async def test_delivery_success_after_retry_emits_no_delivery_failed(wired, monkeypatch):
    # A transient failure that RECOVERS on the retry (the second attempt delivers and
    # the answer lands) is not a terminal abandonment: no ``delivery_failed`` event.
    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    _tune(monkeypatch, wired, delivery_retry_backoff_seconds=0.01)
    attempts: list[ChannelDelivery] = []

    class FlakyChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            attempts.append(delivery)
            if len(attempts) == 1:
                raise ChannelDeliveryError("provider throttled", retryable=True)
            resp = await router.callback(
                _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "second"}')
            )
            assert resp.status_code == 200

    app._channel_registry.reset()
    tai42_app.channels.register("flaky", FlakyChannel())
    try:
        assert await ask_user("q", channel="flaky", timeout=5) == "second"
    finally:
        app._channel_registry.reset()

    assert len(attempts) == 2
    assert hooks.events == []  # the recovered delivery never states a terminal failure


async def test_cancelled_delivery_emits_no_delivery_failed(wired, monkeypatch):
    # Cancellation is not a delivery failure — the caller went away — so the terminal
    # path propagates the CancelledError WITHOUT stating a ``delivery_failed`` event.
    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)

    class CancelledChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            raise asyncio.CancelledError

    app._channel_registry.reset()
    tai42_app.channels.register("cancelled", CancelledChannel())
    try:
        with pytest.raises(asyncio.CancelledError):
            await ask_user("q", channel="cancelled", timeout=5)
    finally:
        app._channel_registry.reset()

    assert hooks.events == []


async def test_failed_delivery_ticket_unclaimable(wired):
    # After the prune, the minted ticket resolves to a dead interaction: the
    # callback door answers the uniform 404 — no late answer can resurrect it.
    captured: dict = {}

    class CapturingFailer(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            captured["delivery"] = delivery
            raise ChannelDeliveryError("send failed")

    app._channel_registry.reset()
    tai42_app.channels.register("capfail", CapturingFailer())
    try:
        with pytest.raises(ChannelDeliveryError):
            await ask_user("q", channel="capfail", timeout=5)
    finally:
        app._channel_registry.reset()

    resp = await router.callback(
        _make_request("POST", path_params={"ticket": _ticket(captured["delivery"])}, body=b'{"answer": "late"}')
    )
    assert resp.status_code == 404


async def test_delivery_failure_after_recorded_answer_falls_through(wired):
    # A fast reply can land before the delivery failure surfaces (e.g. the
    # provider accepted the message, the human answered, and only then the send
    # call errored). ``prune_pending`` then reports already-answered — the
    # recorded answer is returned, never discarded, and nothing raises.
    class AnswerThenFail(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            resp = await router.callback(
                _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "fast"}')
            )
            assert resp.status_code == 200
            raise ChannelDeliveryError("send failed after the reply landed")

    app._channel_registry.reset()
    tai42_app.channels.register("racy", AnswerThenFail())
    try:
        assert await ask_user("q", channel="racy", timeout=5) == "fast"
    finally:
        app._channel_registry.reset()


async def test_hung_delivery_times_out_prunes_and_raises(wired):
    # ``deliver`` is one sub-second send attempt bounded by the ask's whole
    # timeout budget; a deliver still running at that deadline is a hung plugin,
    # so the ask fails loudly instead of blocking forever on the send.
    class HungChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            await asyncio.sleep(60)

    app._channel_registry.reset()
    tai42_app.channels.register("hung", HungChannel())
    try:
        with pytest.raises(ChannelDeliveryError, match=r"delivery timed out after .*interaction"):
            await ask_user("q", channel="hung", timeout=0.05)

        # The persisted question was pruned: nothing open, nothing claimable.
        assert await wired.store.count_open(wired.fake) == 0
    finally:
        app._channel_registry.reset()


async def test_retryable_failure_retries_and_the_second_attempt_delivers(wired, monkeypatch):
    # A transient failure is re-attempted inside the ask's budget; the question
    # stays open between attempts (never pruned), so the retry's delivery still
    # carries a claimable ticket and the loop closes normally.
    _tune(monkeypatch, wired, delivery_retry_backoff_seconds=0.01)
    attempts: list[Any] = []

    class FlakyChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            attempts.append(delivery)
            if len(attempts) == 1:
                raise ChannelDeliveryError("provider throttled", retryable=True)
            # Still persisted: the intermediate failure pruned nothing.
            assert await wired.store.get_state(wired.fake, delivery.interaction_id) is not None
            resp = await router.callback(
                _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "second"}')
            )
            assert resp.status_code == 200

    app._channel_registry.reset()
    tai42_app.channels.register("flaky", FlakyChannel())
    try:
        assert await ask_user("q", channel="flaky", timeout=5) == "second"
    finally:
        app._channel_registry.reset()

    assert len(attempts) == 2
    assert attempts[0].interaction_id == attempts[1].interaction_id  # the same question, re-sent


async def test_non_retryable_failure_is_not_retried(wired, monkeypatch):
    # The default classification: one attempt, then prune + raise. An unknown
    # fault is never blind-retried.
    _tune(monkeypatch, wired, delivery_retry_backoff_seconds=0.01)
    attempts: list[ChannelDelivery] = []

    class HardFailer(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            attempts.append(delivery)
            raise ChannelDeliveryError("recipient refused")

    app._channel_registry.reset()
    tai42_app.channels.register("hard", HardFailer())
    try:
        with pytest.raises(ChannelDeliveryError, match="recipient refused"):
            await ask_user("q", channel="hard", timeout=5)
    finally:
        app._channel_registry.reset()

    assert len(attempts) == 1
    assert await wired.store.count_open(wired.fake) == 0


async def test_retries_exhaust_the_attempt_cap_then_prune_and_raise(wired, monkeypatch):
    # ``delivery_max_attempts`` is the total attempt count; the LAST error is the
    # one that surfaces, and only then is the question pruned.
    _tune(monkeypatch, wired, delivery_max_attempts=2, delivery_retry_backoff_seconds=0.01)
    attempts: list[ChannelDelivery] = []

    class AlwaysThrottled(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            attempts.append(delivery)
            raise ChannelDeliveryError(f"throttled #{len(attempts)}", retryable=True)

    app._channel_registry.reset()
    tai42_app.channels.register("throttled", AlwaysThrottled())
    try:
        with pytest.raises(ChannelDeliveryError, match="throttled #2"):
            await ask_user("q", channel="throttled", timeout=5)
    finally:
        app._channel_registry.reset()

    assert len(attempts) == 2
    assert await wired.store.count_open(wired.fake) == 0


async def test_retry_after_wins_over_the_backoff(wired, monkeypatch):
    # The medium asked for longer than the computed backoff — its wait is honored.
    _tune(monkeypatch, wired, delivery_max_attempts=2, delivery_retry_backoff_seconds=0.001)
    asked_for = 0.08
    at: list[float] = []

    class RetryAfterChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            at.append(asyncio.get_running_loop().time())
            raise ChannelDeliveryError("slow down", retryable=True, retry_after=asked_for)

    app._channel_registry.reset()
    tai42_app.channels.register("slowdown", RetryAfterChannel())
    try:
        with pytest.raises(ChannelDeliveryError, match="slow down"):
            await ask_user("q", channel="slowdown", timeout=5)
    finally:
        app._channel_registry.reset()

    assert len(at) == 2
    assert at[1] - at[0] >= asked_for  # the retry_after wait, not the 1ms backoff


async def test_no_retry_once_the_budget_cannot_hold_the_wait(wired, monkeypatch):
    # Attempts remain, but the ask's budget cannot hold the backoff wait: no
    # further attempt is made — prune + raise the failure at hand.
    _tune(monkeypatch, wired, delivery_max_attempts=5, delivery_retry_backoff_seconds=10)
    attempts: list[ChannelDelivery] = []

    class AlwaysThrottled(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            attempts.append(delivery)
            raise ChannelDeliveryError("throttled", retryable=True)

    app._channel_registry.reset()
    tai42_app.channels.register("nobudget", AlwaysThrottled())
    try:
        with pytest.raises(ChannelDeliveryError, match="throttled"):
            await ask_user("q", channel="nobudget", timeout=0.2)
    finally:
        app._channel_registry.reset()

    assert len(attempts) == 1
    assert await wired.store.count_open(wired.fake) == 0


async def test_cancelled_delivery_is_never_retried(wired, monkeypatch):
    # Cancellation is not a transient send failure: no retry, prune, propagate.
    _tune(monkeypatch, wired, delivery_max_attempts=3, delivery_retry_backoff_seconds=0.01)
    attempts: list[ChannelDelivery] = []

    class CancelledChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            attempts.append(delivery)
            raise asyncio.CancelledError

    app._channel_registry.reset()
    tai42_app.channels.register("cancelled", CancelledChannel())
    try:
        with pytest.raises(asyncio.CancelledError):
            await ask_user("q", channel="cancelled", timeout=5)
    finally:
        app._channel_registry.reset()

    assert len(attempts) == 1
    assert await wired.store.count_open(wired.fake) == 0


async def test_hung_delivery_that_recorded_an_answer_times_out_at_the_budget(wired):
    # The reply lands while deliver is still hanging, then the hang consumes the
    # rest of the ONE ask budget. The answer wait gets what is left — nothing — so
    # the ask times out at the budget rather than blocking past it (the durable
    # answered state remains, but this caller is not held open beyond its deadline).
    class AnswerThenHang(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            resp = await router.callback(
                _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "fast"}')
            )
            assert resp.status_code == 200
            await asyncio.sleep(60)

    app._channel_registry.reset()
    tai42_app.channels.register("hang", AnswerThenHang())
    try:
        with pytest.raises(InteractionTimeoutError, match="an answer was recorded after the budget"):
            await ask_user("q", channel="hang", timeout=0.3)
    finally:
        app._channel_registry.reset()


# -- one budget for the whole ask: delivery time shrinks the answer wait --------


async def test_delivery_time_shrinks_the_answer_wait(wired, monkeypatch):
    # ONE deadline bounds the whole ask — delivery attempts, their backoff, AND the
    # answer wait together — so a delivery phase that consumed measurable time leaves
    # the reply wait strictly LESS than the full budget.
    original = InteractionStore.wait_for_reply
    captured: dict[str, float] = {}

    async def capturing(self, r, reply_to, timeout_seconds, grace_seconds):
        captured["timeout"] = timeout_seconds
        return await original(self, r, reply_to, timeout_seconds, grace_seconds)

    monkeypatch.setattr(InteractionStore, "wait_for_reply", capturing)
    delivery_cost = 0.1

    class SlowDeliverChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            await asyncio.sleep(delivery_cost)  # measurable delivery time
            resp = await router.callback(
                _make_request("POST", path_params={"ticket": _ticket(delivery)}, body=b'{"answer": "ok"}')
            )
            assert resp.status_code == 200

    app._channel_registry.reset()
    tai42_app.channels.register("slowdeliver", SlowDeliverChannel())
    try:
        assert await ask_user("q", channel="slowdeliver", timeout=5) == "ok"
    finally:
        app._channel_registry.reset()

    assert captured["timeout"] < 5  # not the full budget
    assert captured["timeout"] <= 5 - delivery_cost  # the delivery time was subtracted


async def test_delivery_that_eats_the_budget_times_out_without_a_forever_wait(wired, monkeypatch):
    # A delivery that consumes effectively the whole budget leaves only a shrunken
    # answer wait: the ask times out, the question is pruned, and the reply wait is
    # never handed a non-positive timeout (which redis BLPOP would read as
    # block-forever).
    original = InteractionStore.wait_for_reply
    timeouts: list[float] = []

    async def capturing(self, r, reply_to, timeout_seconds, grace_seconds):
        timeouts.append(timeout_seconds)
        return await original(self, r, reply_to, timeout_seconds, grace_seconds)

    monkeypatch.setattr(InteractionStore, "wait_for_reply", capturing)

    class BudgetEatingChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            # Return successfully but only after most of the budget is gone; no
            # answer is posted, so the shrunken wait finds nothing.
            await asyncio.sleep(0.2)

    app._channel_registry.reset()
    tai42_app.channels.register("budgeteater", BudgetEatingChannel())
    try:
        with pytest.raises(InteractionTimeoutError, match="with no answer"):
            await ask_user("q", channel="budgeteater", timeout=0.3)
    finally:
        app._channel_registry.reset()

    assert await wired.store.count_open(wired.fake) == 0  # pruned
    # BLPOP is never asked to block forever: the wait is either skipped (delivery ate
    # the whole budget) or handed the shrunken remainder — never a non-positive value.
    assert all(0 < t < 0.3 for t in timeouts)


async def test_timeout_when_record_already_gone_reports_gone(wired, monkeypatch):
    # TOCTOU at the timeout prune: the question record has already vanished
    # (expired, or the pending-list reconciler pruned a deadline-crossed question)
    # before the timeout prune runs, so the prune reports "gone". The timeout
    # message must name that state — never claim a late answer was recorded.
    async def gone(self, r, interaction_id, group_id):
        return "gone"

    monkeypatch.setattr(InteractionStore, "prune_pending", gone)

    class SilentChannel(DeliverOnlyChannel):
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return  # delivered, but no answer is ever posted

    app._channel_registry.reset()
    tai42_app.channels.register("silent", SilentChannel())
    try:
        with pytest.raises(InteractionTimeoutError, match="already gone"):
            await ask_user("q", channel="silent", timeout=0.05)
    finally:
        app._channel_registry.reset()


# -- async-park callback-door parity ------------------------------------------


@pytest.fixture
def async_driver():
    # A bound resuming driver: the resume continuation tool + the execution identity
    # the park's continuation is later rebound as. Both are reset after the test.
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    yield
    reset_execution_identity(id_token)
    reset_resume_continuation_tool(tool_token)


async def test_async_park_callback_door_answerable_through_reaper_margin(
    monkeypatch, wired, fake_channel, async_driver
):
    # Door parity for an async park: the channel-delivery callback ticket carries the
    # SAME reaper margin the state hash does, so the public callback door and the
    # authenticated /answer door stay answerable through the identical
    # (expiry_at, expiry_at + park_ttl_margin) window — never one door rejecting a
    # late answer the other still accepts. ``idle_ttl`` is shrunk below the expiry
    # horizon so the state's own ``_key_ttl`` is exactly ``expiry_at + margin`` (not
    # floored to idle_ttl), and the reaper interval fixes margin = 2*ceil(45) = 90s.
    # The continuation is stubbed — this exercises the door, not the resume.
    _tune(monkeypatch, wired, idle_ttl_seconds=10, expiry_reaper_interval_seconds=45)
    resumed: list[Any] = []

    async def _stub(identity, fingerprint, tool, interaction_id, answer):
        resumed.append(answer)

    monkeypatch.setattr(continuation_module, "_run_continuation", _stub)

    expiry = datetime.now(UTC) + timedelta(seconds=200)
    result = await ask_user("Approve?", channel="fake", mode="async", expiry_at=expiry)
    assert isinstance(result, SuspendedInteraction)
    ticket = _ticket(fake_channel.deliveries[0])

    # Into the window: PAST expiry_at (200s) but WITHIN the reaper margin
    # (expiry_at + 90 = 290s). Without the margin on the ticket, ``resolve_ticket``
    # would now return None and the callback door would 404 — while the state hash,
    # and therefore the authenticated door, is still live.
    wired.fake.advance(245)
    assert await wired.store.get_state(wired.fake, result.interaction_id) is not None

    resp = await router.callback(_make_request("POST", path_params={"ticket": ticket}, body=b'{"answer": "yes"}'))
    assert resp.status_code == 200
    assert json.loads(bytes(resp.body))["data"]["status"] == "answered"
    # The claim fired the stored continuation once, on its detached task.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert resumed == ["yes"]
