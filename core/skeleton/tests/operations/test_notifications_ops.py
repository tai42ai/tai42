"""Op-level oracles for the notifications operations.

``notify_user`` forwards every argument verbatim to the channels helper, returns the
bare confirmation string, and NEVER swallows a failure — mapping the helper's loud
errors to the operation's typed errors (ValueError→400, ChannelInputError→400,
NotImplementedError→501, ChannelDeliveryError→502/503 (retryable→503)). ``list_notifications`` reads the
sink, and the destructive
projection carries ``destructiveHint``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager

import pytest
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id
from tai42_contract.app import tai42_app
from tai42_contract.channels import (
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
    ChannelTemplate,
)
from tai42_contract.interactions.models import MediaItem, MediaKind
from tai42_contract.manifest import ApiToolsConfig

from tai42_skeleton.access_control.request_scopes import (
    reset_request_identity_claims,
    set_request_identity_claims,
)
from tai42_skeleton.app.instance import app
from tai42_skeleton.channels import notifications_sink
from tai42_skeleton.operations import (
    BadRequestError,
    ForbiddenError,
    NotSupportedError,
    OperationRegistry,
    UnavailableError,
    UpstreamError,
    operation_metadata_of,
)
from tai42_skeleton.operations import notifications as notifications_ops
from tai42_skeleton.operations.projection import project_operations


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # the interactions surface is OFF with no Redis. These tests exercise the ON
    # feature, so configure its store — the fake connection still stands in; only the
    # presence gate reads this env var.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@contextmanager
def _restricted(own_id: str, owner: str | None = None) -> Iterator[None]:
    """Bind a RESTRICTED owned-key caller isolated to its OWN id ``own_id``. The owner
    claim (a DISTINCT ``owner-of-{own_id}`` by default) is what MARKS the caller
    restricted, but the isolation identity is its own id — each key is its own island —
    so the write clamp scopes writes to ``own_id``, never the owner."""
    owner_claim = owner if owner is not None else f"owner-of-{own_id}"
    claims_token = set_request_identity_claims({OWNER_USER_ID_CLAIM: owner_claim})
    uid_token = set_request_user_id(own_id)
    try:
        yield
    finally:
        reset_request_user_id(uid_token)
        reset_request_identity_claims(claims_token)


@pytest.fixture
def sink_redis(monkeypatch, fake_redis):
    """Point the internal notifications sink's Redis at the shared fake so the real
    helper's ``channel=None`` writes land somewhere readable back in the test."""

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(notifications_sink, "client_ctx", _ctx)
    return fake_redis


class _RecordingHelper:
    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self._raise = raise_exc

    async def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))
        if self._raise is not None:
            raise self._raise


# -- notify_user --------------


async def test_notify_user_forwards_arguments_and_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _RecordingHelper()
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    result = await notifications_ops.notify_user("Deploy finished", channel="telegram", recipient="@ops")

    assert result == "notification sent via 'telegram'"
    assert helper.calls == [
        (
            ("Deploy finished",),
            {
                "channel": "telegram",
                "recipient": "@ops",
                "audience": None,
                "media": None,
                "template": None,
                "options": None,
            },
        )
    ]


async def test_notify_user_defaults_forwarded_and_maps_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    # The op forwards ``channel=None``/``recipient=None`` verbatim; the helper's
    # missing-channel ValueError is mapped to a loud BadRequestError (400), never
    # swallowed.
    helper = _RecordingHelper(raise_exc=ValueError("no internal notifications surface configured; name a channel"))
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    with pytest.raises(BadRequestError, match="no internal notifications surface configured"):
        await notifications_ops.notify_user("hello")

    assert helper.calls == [
        (
            ("hello",),
            {"channel": None, "recipient": None, "audience": None, "media": None, "template": None, "options": None},
        )
    ]


async def test_notify_user_rejects_caller_sender_identity_as_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # sender_identity is internal to the conversation bridge: a caller-supplied value is
    # a 400 BEFORE any send, so the channels helper is never reached.
    helper = _RecordingHelper()
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    with pytest.raises(BadRequestError, match="sender_identity is set internally"):
        await notifications_ops.notify_user("hi", channel="telegram", sender_identity="+15550001")

    assert helper.calls == []


async def test_notify_user_channel_omitted_records_to_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _RecordingHelper()
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    result = await notifications_ops.notify_user("hi")

    assert result == "notification recorded to the internal sink"


async def test_notify_user_propagates_delivery_failure_as_502(monkeypatch: pytest.MonkeyPatch) -> None:
    # A permanent delivery refusal (retryable defaults False) is the non-retryable 502.
    helper = _RecordingHelper(raise_exc=ChannelDeliveryError("provider unreachable"))
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    with pytest.raises(UpstreamError, match="provider unreachable"):
        await notifications_ops.notify_user("hello", channel="telegram")


async def test_notify_user_retryable_delivery_failure_maps_to_503_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transient delivery failure the medium said to retry after N seconds is a 503,
    # distinct from the permanent 502, and the wait is propagated in the error's extra so
    # a caller can honor it.
    helper = _RecordingHelper(raise_exc=ChannelDeliveryError("rate limited", retryable=True, retry_after=12.0))
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    with pytest.raises(UnavailableError, match="rate limited") as exc_info:
        await notifications_ops.notify_user("hello", channel="telegram")
    assert exc_info.value.extra == {"retry_after": 12.0}


async def test_notify_user_retryable_delivery_failure_without_retry_after_maps_to_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transient failure with no medium-supplied retry_after is still a 503, carrying no
    # retry_after key — nothing to propagate.
    helper = _RecordingHelper(raise_exc=ChannelDeliveryError("transient blip", retryable=True))
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    with pytest.raises(UnavailableError, match="transient blip") as exc_info:
        await notifications_ops.notify_user("hello", channel="telegram")
    assert exc_info.value.extra == {}


async def test_notify_user_permanent_input_refusal_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # A channel's permanent refusal of the input's shape (retrying cannot succeed) is a
    # client error (400), never the 502 a permanent delivery failure maps to.
    helper = _RecordingHelper(raise_exc=ChannelInputError("cannot render a data: image URL"))
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    with pytest.raises(BadRequestError, match="data: image URL"):
        await notifications_ops.notify_user("hello", channel="web")


async def test_notify_user_channel_cannot_notify_is_501(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _RecordingHelper(raise_exc=NotImplementedError("this channel cannot notify"))
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    with pytest.raises(NotSupportedError, match="cannot notify"):
        await notifications_ops.notify_user("hello", channel="webhook")


async def test_notify_user_forwards_media_and_template(monkeypatch: pytest.MonkeyPatch) -> None:
    # The two richer-send fields are forwarded verbatim to the channels helper.
    helper = _RecordingHelper()
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)
    media = [MediaItem(kind=MediaKind.IMAGE, url="https://example.com/a.png")]
    template = ChannelTemplate(name="status_update", language="en_US", parameters=["A-42"])

    await notifications_ops.notify_user("hi", channel="whatsapp", media=media)
    await notifications_ops.notify_user("done", channel="whatsapp", template=template)

    assert helper.calls == [
        (
            ("hi",),
            {
                "channel": "whatsapp",
                "recipient": None,
                "audience": None,
                "media": media,
                "template": None,
                "options": None,
            },
        ),
        (
            ("done",),
            {
                "channel": "whatsapp",
                "recipient": None,
                "audience": None,
                "media": None,
                "template": template,
                "options": None,
            },
        ),
    ]


async def test_notify_user_forwards_options(monkeypatch: pytest.MonkeyPatch) -> None:
    # The options field is forwarded verbatim to the channels helper.
    helper = _RecordingHelper()
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    await notifications_ops.notify_user("pick one", channel="web", options=["Item A", "Item B"])

    assert helper.calls == [
        (
            ("pick one",),
            {
                "channel": "web",
                "recipient": None,
                "audience": None,
                "media": None,
                "template": None,
                "options": ["Item A", "Item B"],
            },
        )
    ]


async def test_notify_user_media_without_channel_maps_valueerror_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # The helper's channel=None refusal for media/template surfaces as a loud 400.
    helper = _RecordingHelper(raise_exc=ValueError("media and template require a named channel; the internal sink..."))
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    with pytest.raises(BadRequestError, match="require a named channel"):
        await notifications_ops.notify_user(
            "photo", media=[MediaItem(kind=MediaKind.IMAGE, url="https://example.com/a.png")]
        )


async def test_notify_user_capability_gap_maps_not_implemented_to_501(monkeypatch: pytest.MonkeyPatch) -> None:
    # A channel that does not advertise the media/template capability is a 501 capability
    # gap — the same 501 vocabulary as a channel that cannot notify at all.
    helper = _RecordingHelper(raise_exc=NotImplementedError("channel 'telegram' does not support media notifications"))
    monkeypatch.setattr(notifications_ops, "_notify_user", helper)

    with pytest.raises(NotSupportedError, match="does not support media notifications"):
        await notifications_ops.notify_user(
            "photo", channel="telegram", media=[MediaItem(kind=MediaKind.IMAGE, url="https://example.com/a.png")]
        )


# -- write-side isolation clamp: a restricted caller touches ONLY its own feed ----
# Exercised end-to-end through the operation door over the REAL channels helper +
# sink (no monkeypatched helper), so the clamp is proven on the operation surface.


async def test_restricted_notify_rejects_other_identity_as_403(sink_redis) -> None:
    # A restricted bob addressing alice is a cross-identity injection: the helper's
    # loud CrossIdentityAudienceError surfaces as a 403 authorization denial (the
    # write-side mirror of the read door's ForbiddenError, NOT a 400 bad request) and
    # NOTHING lands in alice's feed (or the shared feed) — the exfil/inject path is
    # closed at the write door.
    with _restricted("bob"), pytest.raises(ForbiddenError, match="may address only its own identity"):
        await notifications_ops.notify_user("hi alice", audience="alice")

    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_restricted_notify_rejects_own_owner_as_403(sink_redis) -> None:
    # Under key-keyed isolation each key is its own island: the caller's OWN OWNER is
    # a FOREIGN write target, no longer a privileged one. A restricted bob (own id
    # bob, owner-claim alice) addressing its owner alice is rejected exactly like any
    # other cross-identity inject — a loud 403 — and NOTHING lands in alice's (the
    # owner's) feed or the shared feed. This pins that the owner lost its former write
    # privilege; an owner-privileged write model would let this through.
    with _restricted("bob", owner="alice"), pytest.raises(ForbiddenError, match="may address only its own identity"):
        await notifications_ops.notify_user("hi owner", audience="alice")

    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


class _RecordingChannel:
    """Records every notification handed to ``notify`` — proves whether the channel
    path was reached. ``notify_user`` never calls ``deliver`` (that is the ``ask_user``
    surface), so its protocol stub asserts if ever reached."""

    def __init__(self) -> None:
        self.notifications: list[ChannelNotification] = []

    async def deliver(self, delivery: object) -> None:
        raise AssertionError("notify_user must never call deliver")

    async def notify(self, notification: ChannelNotification) -> list[str]:
        self.notifications.append(notification)
        return []


async def test_restricted_notify_channel_path_rejects_other_identity_as_403(sink_redis) -> None:
    # The clamp fires on the CHANNEL path too, BEFORE the channel is resolved: a
    # restricted bob addressing alice over a channel is rejected as a 403 and NOTHING
    # leaks — the recording channel is never touched AND alice's feed stays empty. This
    # pins the clamp OUTSIDE the ``if channel is None`` branch: a sink-only clamp would
    # skip the channel path, deliver to the channel, and write alice's feed.
    app._channel_registry.reset()
    channel = _RecordingChannel()
    tai42_app.channels.register("fake", channel)
    try:
        with _restricted("bob"), pytest.raises(ForbiddenError, match="may address only its own identity"):
            await notifications_ops.notify_user("hi alice", channel="fake", audience="alice")
    finally:
        app._channel_registry.reset()

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_restricted_notify_channel_path_rejects_own_owner_as_403(sink_redis) -> None:
    # The owner is a FOREIGN target on the CHANNEL path too: a restricted bob (owner
    # alice) addressing its owner alice over a channel is rejected as a 403 BEFORE the
    # channel is resolved — the recording channel is never touched AND alice's feed
    # stays empty. Under key-keyed isolation the owner holds no channel-write privilege.
    app._channel_registry.reset()
    channel = _RecordingChannel()
    tai42_app.channels.register("fake", channel)
    try:
        with (
            _restricted("bob", owner="alice"),
            pytest.raises(ForbiddenError, match="may address only its own identity"),
        ):
            await notifications_ops.notify_user("hi owner", channel="fake", audience="alice")
    finally:
        app._channel_registry.reset()

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_restricted_notify_scopes_unset_audience_to_self(sink_redis) -> None:
    # An unset audience is scoped to the restricted caller's OWN feed (its own id),
    # never its owner's.
    with _restricted("bob"):
        await notifications_ops.notify_user("status")

    own = await notifications_sink.read_notifications(audience="bob")
    assert len(own) == 1
    assert own[0]["audience"] == "bob"
    # Nothing leaked into another identity's feed.
    assert await notifications_sink.read_notifications(audience="alice") == []


async def test_unrestricted_notify_may_address_any_identity(sink_redis) -> None:
    # Regression guard: an unrestricted caller (no bound owner claim) is NOT clamped
    # — it may address any identity, exactly as before.
    await notifications_ops.notify_user("hi alice", audience="alice")

    own = await notifications_sink.read_notifications(audience="alice")
    assert len(own) == 1
    assert own[0]["audience"] == "alice"


# -- list_notifications -------------------------------------------------------


async def test_list_notifications_returns_sink_records(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [{"id": "2", "message": "b"}, {"id": "1", "message": "a"}]

    async def _read(audience: str | None = None) -> list:
        return records

    monkeypatch.setattr(notifications_ops, "read_notifications", _read)
    assert await notifications_ops.list_notifications() == {"notifications": records}


# -- projection ---------------------------------------------------------------


def test_notify_user_projects_with_destructive_hint() -> None:
    # notify_user is destructive (an external side-effect) — off the default surface
    # but includable; when projected it carries the destructiveHint annotation.
    reg = OperationRegistry()
    reg.register(operation_metadata_of(notifications_ops.notify_user))

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, dict] = {}

        def tool(self, *, force, name, tags, annotations):
            self.registered[name] = {"annotations": annotations}
            return lambda fn: fn

    class _App:
        def __init__(self) -> None:
            self.tools = _Rec()

    app = _App()
    names = project_operations(app, ApiToolsConfig(expose_destructive=True), registry=reg)
    assert "notify_user" in names
    assert app.tools.registered["notify_user"]["annotations"].destructiveHint is True


# -- the store-unconfigured OFF gate ------------------------------------
# The internal feed lives on the interactions Redis; with none configured the read is
# honestly empty and a channel-less send refuses with the feature's named 501. Force
# OFF by delenv-ing BOTH vars, overriding the autouse setenv.


async def test_list_notifications_off_when_store_unconfigured_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no store the honest answer is the empty collection — no store touched.
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    assert await notifications_ops.list_notifications() == {"notifications": []}


async def test_notify_user_off_when_store_unconfigured_raises_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    # A channel-less send targets the internal sink; with no store it refuses with the
    # named 501 code, served by the boundary guard at the sole feed writer
    # (record_notification) — the record write is the first store access on this path.
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    with pytest.raises(NotSupportedError) as exc_info:
        await notifications_ops.notify_user("status update", channel=None)
    assert exc_info.value.extra["code"] == "interactions-not-configured"


async def test_notify_user_off_restricted_channel_send_refuses_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A restricted (owned-key) caller's audience is clamped to its own identity, so its
    # channel send ALWAYS records to the in-app feed too. With the interactions store
    # OFF that feed write refuses with the named 501 at the boundary guard BEFORE the
    # channel is notified — no phantom feed entry, no partial delivery. This pins the
    # gate at the store-access choke point, not only the channel-less path.
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    app._channel_registry.reset()
    channel = _RecordingChannel()
    tai42_app.channels.register("fake", channel)
    try:
        with _restricted("bob"), pytest.raises(NotSupportedError) as exc_info:
            await notifications_ops.notify_user("status", channel="fake")
    finally:
        app._channel_registry.reset()

    assert exc_info.value.extra["code"] == "interactions-not-configured"
    # The channel was NEVER notified — the refuse fires before delivery.
    assert channel.notifications == []


async def test_notify_user_off_unrestricted_channel_send_no_audience_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unrestricted caller's channel send with no audience never touches the in-app
    # feed, so it delivers normally even with the store OFF — the boundary guard is
    # reached only when a feed write actually happens.
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    app._channel_registry.reset()
    channel = _RecordingChannel()
    tai42_app.channels.register("fake", channel)
    try:
        result = await notifications_ops.notify_user("hi", channel="fake")
    finally:
        app._channel_registry.reset()

    assert result == "notification sent via 'fake'"
    assert len(channel.notifications) == 1
