"""The shared inbound-answer ladder ``handle_inbound_answer``.

The one ladder every correlated channel will share: a guest reply on a
correlation key is forwarded to the interaction answer door, and the door's
2xx/404/400/other status drives forward / bridge / retry-in-place / raise — over
the minimal :class:`~tai42_contract.channels.CorrelationStore` port, with the
guest notice, the operator alert and the conversation bridge as the observable
side effects.

The handler's seams are faked at the module boundary (the router-test pattern):
``_forward_answer`` returns a real ``httpx.Response`` so the door-body parse runs
for real, the conversation bridge is captured on the app's ``_conversation_accept``
seam, the guest channel is a registered recording fake, and the operator alert is
a PLATFORM EVENT captured on the hooks manager's ``on_event``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, ChannelNotification, Correlation
from tai42_contract.conversations import BlankInboundTextError

from tai42_skeleton.app.instance import app
from tai42_skeleton.channels import inbound as inbound_module
from tai42_skeleton.channels.inbound import (
    ANSWER_REJECTED_EVENT_TOPIC,
    AnswerForwardError,
    InboundAnswerOutcome,
    InboundBridge,
    handle_inbound_answer,
)
from tai42_skeleton.hooks import cache as hooks_cache
from tai42_skeleton.interactions.settings import InteractionsSettings

_CALLBACK_URL = "https://host.example/api/interactions/callback/tkt"
_INTERACTION_ID = "int-42"


class FakeStore:
    """A :class:`CorrelationStore` over one in-memory record; records releases."""

    def __init__(self, entry: Correlation | None) -> None:
        self._entry = entry
        self.released: list[str] = []

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        return True

    async def get_correlation(self, key: str) -> Correlation | None:
        return self._entry

    async def release_correlation(self, key: str) -> None:
        self.released.append(key)


class RecordingChannel:
    """A registered channel that records every guest notice handed to ``notify``."""

    def __init__(self) -> None:
        self.notifications: list[ChannelNotification] = []

    async def deliver(self, delivery: ChannelDelivery) -> None:  # pragma: no cover - unused
        return None

    async def notify(self, notification: ChannelNotification) -> list[str]:
        self.notifications.append(notification)
        return []


class FailingNotifyChannel:
    """A channel whose ``notify`` raises — proves a notify failure is non-fatal."""

    def __init__(self) -> None:
        self.calls = 0

    async def deliver(self, delivery: ChannelDelivery) -> None:  # pragma: no cover - unused
        return None

    async def notify(self, notification: ChannelNotification) -> list[str]:
        self.calls += 1
        raise RuntimeError("provider unreachable")


class FakeHooksManager:
    """Captures every ``on_event`` the handler emits."""

    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    async def on_event(self, topic, payload, *, tool_kwargs_override=None):
        self.events.append(SimpleNamespace(topic=topic, payload=payload))


def _entry(**overrides) -> Correlation:
    base = {
        "callback_url": _CALLBACK_URL,
        "interaction_id": _INTERACTION_ID,
        "ttl_deadline": datetime.now(UTC) + timedelta(minutes=5),
    }
    base.update(overrides)
    return Correlation(**base)


def _bridge(*, owns_retry_notice: bool = False) -> InboundBridge:
    return InboundBridge(
        channel_id="fakechan",
        our_identity="op-1",
        client_address="+15550001111",
        cap_key="+15550001111",
        provider_message_id="prov-msg-1",
        bridge_text="hello there",
        owns_retry_notice=owns_retry_notice,
    )


@pytest.fixture
def wired(monkeypatch):
    """Register a recording guest channel and capture the conversation-bridge and
    operator-event seams. Yields a namespace of the captured side effects."""
    app._channel_registry.reset()
    channel = RecordingChannel()
    tai42_app.channels.register("fakechan", channel)

    accept_calls: list[SimpleNamespace] = []

    async def _fake_accept(
        channel_id,
        our_identity,
        client_address,
        cap_key,
        text,
        provider_message_id,
        params=None,
        form=None,
        attachments=None,
        location=None,
    ):
        accept_calls.append(
            SimpleNamespace(
                channel=channel_id,
                our_identity=our_identity,
                client_address=client_address,
                cap_key=cap_key,
                text=text,
                provider_message_id=provider_message_id,
                params=params,
            )
        )
        return "bridged-msg-1"

    monkeypatch.setattr(app, "_conversation_accept", _fake_accept)

    hooks = FakeHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)

    # The host-pin fails CLOSED when the public base is unset, so configure it to the
    # host _CALLBACK_URL is on; the pin-specific tests re-point it via _pin_public_base.
    _pin_public_base(monkeypatch, "https://host.example")

    yield SimpleNamespace(channel=channel, accept_calls=accept_calls, events=hooks.events)
    app._channel_registry.reset()


def _stub_forward(monkeypatch, response: httpx.Response | None = None, *, raises: Exception | None = None):
    """Replace the httpx forward seam: return ``response`` or raise ``raises``.
    Records the (callback_url, answer, params) it was called with."""
    calls: list[SimpleNamespace] = []

    async def _fake(callback_url, answer, params=None):
        calls.append(SimpleNamespace(callback_url=callback_url, answer=answer, params=params))
        if raises is not None:
            raise raises
        assert response is not None
        return response

    monkeypatch.setattr(inbound_module, "_forward_answer", _fake)
    return calls


# -- 1. miss: side-effect-free ---------------------------------------------------


async def test_no_correlation_returns_and_touches_nothing(wired, monkeypatch):
    store = FakeStore(None)
    forward_calls = _stub_forward(monkeypatch, httpx.Response(200))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="hi", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.NO_CORRELATION
    # Zero side effects on the miss path: no forward, no release, no bridge, no
    # notice, no event.
    assert forward_calls == []
    assert store.released == []
    assert wired.channel.notifications == []
    assert wired.accept_calls == []
    assert wired.events == []


# -- 2. 2xx: forwarded + released ------------------------------------------------


async def test_2xx_releases_and_forwards(wired, monkeypatch):
    store = FakeStore(_entry())
    forward_calls = _stub_forward(monkeypatch, httpx.Response(200))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="yes", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.FORWARDED
    assert result.retry_reason is None  # a clean forward carries no door reason
    assert result.retry_field is None
    # The answer reached the door verbatim; the correlation was released; nothing
    # else happened.
    assert forward_calls == [SimpleNamespace(callback_url=_CALLBACK_URL, answer="yes", params=None)]
    assert store.released == ["k"]
    assert wired.channel.notifications == []
    assert wired.accept_calls == []
    assert wired.events == []


# -- 3. 404: released + bridged --------------------------------------------------


async def test_404_releases_and_bridges(wired, monkeypatch):
    store = FakeStore(_entry())
    _stub_forward(monkeypatch, httpx.Response(404, json={"error": "not found"}))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="late reply", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.BRIDGED
    assert result.retry_reason is None  # a gone-ask 404 carries no door reason
    assert store.released == ["k"]
    # The reply is bridged as a fresh turn with the bridge fields verbatim.
    assert len(wired.accept_calls) == 1
    call = wired.accept_calls[0]
    assert call.channel == "fakechan"
    assert call.our_identity == "op-1"
    assert call.client_address == "+15550001111"
    assert call.cap_key == "+15550001111"
    assert call.text == "hello there"
    assert call.provider_message_id == "prov-msg-1"
    # A gone ask needs no guest notice or operator event — it is a normal bridge.
    assert wired.channel.notifications == []
    assert wired.events == []


# -- 4. 400 retryable: KEPT + guest notice + one alert ---------------------------


async def test_400_retryable_keeps_correlation_notifies_and_alerts_once(wired, monkeypatch):
    store = FakeStore(_entry())
    _stub_forward(
        monkeypatch,
        httpx.Response(400, json={"error": "Please answer with yes or no.", "field": "reply", "retry_in_place": True}),
    )

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="maybe", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.RETRY_KEPT
    # The door's reason/field ride back on the result for a channel that owns its notice.
    assert result.retry_reason == "Please answer with yes or no."
    assert result.retry_field == "reply"
    # The correlation is KEPT so the guest's next reply resolves the same ask.
    assert store.released == []
    assert wired.accept_calls == []
    # The guest is told what's expected: the notice carries the door's reason and
    # is sent from the ask's identity to the guest address.
    assert len(wired.channel.notifications) == 1
    notice = wired.channel.notifications[0]
    assert "Please answer with yes or no." in notice.message
    assert notice.recipient == "+15550001111"
    assert notice.sender_identity == "op-1"
    # Exactly one platform event on the answer-rejected topic, carrying the full
    # fact payload (channel, interaction, reason, field, retry_in_place=True).
    assert len(wired.events) == 1
    event = wired.events[0]
    assert event.topic == ANSWER_REJECTED_EVENT_TOPIC == "interactions_answer_rejected"
    assert event.payload == {
        "channel": "fakechan",
        "interaction_id": _INTERACTION_ID,
        "client_address": "+15550001111",
        "our_identity": "op-1",
        "reason": "Please answer with yes or no.",
        "field": "reply",
        "retry_in_place": True,
        # Default bridge: core sent the generic notice, so it owns it.
        "notice_owner": "core",
        # Default ask policy is retry (today's behavior).
        "policy": "retry",
    }


# -- 4b. 400 retryable with owns_retry_notice: KEPT, NO core notice, event tagged --


async def test_400_retryable_owns_retry_notice_skips_core_notice_keeps_and_alerts(wired, monkeypatch):
    # A channel that owns its correction surface (owns_retry_notice=True) must NOT be
    # double-messaged: core SKIPS its guest notice, but still keeps the correlation and
    # still emits the operator event tagged notice_owner="channel".
    store = FakeStore(_entry())
    _stub_forward(
        monkeypatch,
        httpx.Response(400, json={"error": "Please pick a listed option.", "field": "choice", "retry_in_place": True}),
    )

    result = await handle_inbound_answer(
        channel_id="fakechan",
        correlation_key="k",
        answer="maybe",
        store=store,
        bridge=_bridge(owns_retry_notice=True),
    )

    assert result.outcome is InboundAnswerOutcome.RETRY_KEPT
    # The door's specific reason/field ride back for the channel to render its own error.
    assert result.retry_reason == "Please pick a listed option."
    assert result.retry_field == "choice"
    # Correlation KEPT so the channel's re-ask resolves the same ask.
    assert store.released == []
    # Core sent NO guest notice — the channel owns it.
    assert wired.channel.notifications == []
    # The operator event still fires, tagged with the channel as the notice owner.
    assert len(wired.events) == 1
    assert wired.events[0].payload["notice_owner"] == "channel"
    assert wired.events[0].payload["retry_in_place"] is True
    assert wired.events[0].payload["reason"] == "Please pick a listed option."


async def test_400_hard_mismatch_ignores_owns_retry_notice_and_core_notices(wired, monkeypatch):
    # owns_retry_notice applies ONLY to the retryable path: a closed ask's correction
    # surface is moot, so core ALWAYS sends the final notice and owns it.
    store = FakeStore(_entry())
    _stub_forward(
        monkeypatch,
        httpx.Response(400, json={"error": "This question is closed.", "retry_in_place": False}),
    )

    result = await handle_inbound_answer(
        channel_id="fakechan",
        correlation_key="k",
        answer="nope",
        store=store,
        bridge=_bridge(owns_retry_notice=True),
    )

    assert result.outcome is InboundAnswerOutcome.BRIDGED
    assert len(wired.channel.notifications) == 1  # core still notices on a hard mismatch
    assert wired.events[0].payload["notice_owner"] == "core"


# -- 5. 400 non-retryable: released + final notice + alert + bridged -------------


async def test_400_non_retryable_releases_notifies_alerts_and_bridges(wired, monkeypatch):
    store = FakeStore(_entry())
    _stub_forward(
        monkeypatch,
        httpx.Response(400, json={"error": "That question no longer accepts this answer.", "retry_in_place": False}),
    )

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="nope", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.BRIDGED
    # A hard-mismatch BRIDGED still carries the door's reason (the ask judged the answer).
    assert result.retry_reason == "That question no longer accepts this answer."
    assert result.retry_field is None
    # The hard-mismatch seam: released, guest told the question is closed, operator
    # alerted, and the reply bridged as a fresh turn.
    assert store.released == ["k"]
    assert len(wired.channel.notifications) == 1
    assert "That question no longer accepts this answer." in wired.channel.notifications[0].message
    # The hard-mismatch event carries retry_in_place=False and core owns the notice.
    assert len(wired.events) == 1
    assert wired.events[0].topic == ANSWER_REJECTED_EVENT_TOPIC
    assert wired.events[0].payload["retry_in_place"] is False
    assert wired.events[0].payload["reason"] == "That question no longer accepts this answer."
    assert wired.events[0].payload["notice_owner"] == "core"
    assert len(wired.accept_calls) == 1
    assert wired.accept_calls[0].text == "hello there"


# -- 6. 5xx: raises, correlation kept, no notice/alert ---------------------------


async def test_5xx_raises_and_keeps_correlation(wired, monkeypatch):
    store = FakeStore(_entry())
    _stub_forward(monkeypatch, httpx.Response(503, text="service unavailable"))

    with pytest.raises(AnswerForwardError, match="HTTP 503"):
        await handle_inbound_answer(
            channel_id="fakechan", correlation_key="k", answer="x", store=store, bridge=_bridge()
        )

    # Kept for the webhook redelivery to re-run the ladder; no guest/operator noise.
    assert store.released == []
    assert wired.channel.notifications == []
    assert wired.events == []
    assert wired.accept_calls == []


async def test_transport_fault_raises_and_keeps_correlation(wired, monkeypatch):
    # A transport fault forwarding to the door is indeterminate — raise loudly, keep
    # the correlation, leave no side effects.
    store = FakeStore(_entry())
    _stub_forward(monkeypatch, raises=httpx.ConnectError("connection refused"))

    with pytest.raises(AnswerForwardError, match="forwarding the answer to the door failed"):
        await handle_inbound_answer(
            channel_id="fakechan", correlation_key="k", answer="x", store=store, bridge=_bridge()
        )

    assert store.released == []
    assert wired.channel.notifications == []
    assert wired.events == []


# -- 7. a notify failure on the 400 path is swallowed ----------------------------


async def test_400_retryable_notify_failure_is_swallowed(monkeypatch):
    # A guest-notice failure must never turn a handled rejection into a lost
    # webhook: the event still fires and RETRY_KEPT still returns.
    app._channel_registry.reset()
    failing = FailingNotifyChannel()
    tai42_app.channels.register("fakechan", failing)

    hooks = FakeHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    _pin_public_base(monkeypatch, "https://host.example")

    store = FakeStore(_entry())
    _stub_forward(monkeypatch, httpx.Response(400, json={"error": "bad", "retry_in_place": True}))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="maybe", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.RETRY_KEPT
    assert failing.calls == 1  # the notice was attempted
    assert store.released == []
    assert len(hooks.events) == 1  # the event still fired despite the notify failure
    app._channel_registry.reset()


# -- door-body parse: a malformed 400 body degrades to retry-in-place ------------


def _pin_public_base(monkeypatch, base: str | None) -> None:
    """Point the handler's host-pinning at ``base`` (the configured public base)."""
    monkeypatch.setattr(inbound_module, "interactions_settings", lambda: InteractionsSettings(public_base_url=base))


# -- security carry-in (a): callback_url host pinning ----------------------------


async def test_callback_host_mismatch_releases_and_treats_as_no_correlation(wired, monkeypatch):
    # A stored callback_url whose host is not the configured public base (a poisoned or
    # stale store entry) must NEVER be POSTed to: the reservation is released, the reply
    # is treated as uncorrelated (the caller bridges it), and nothing is forwarded. A
    # best-effort operator event fires — carrying the reason, NEVER the URL/ticket.
    _pin_public_base(monkeypatch, "https://host.example")
    store = FakeStore(_entry(callback_url="https://evil.example/api/interactions/callback/tkt"))
    forward_calls = _stub_forward(monkeypatch, httpx.Response(200))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="hi", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.NO_CORRELATION
    assert forward_calls == []  # the guest's answer was never shipped to the wrong host
    assert store.released == ["k"]  # the poisoned reservation is dropped
    assert wired.channel.notifications == []
    assert wired.accept_calls == []
    # The callback-discarded event fired with the right payload and NO url/ticket.
    assert len(wired.events) == 1
    event = wired.events[0]
    assert event.topic == inbound_module.CALLBACK_DISCARDED_EVENT_TOPIC == "interactions_callback_discarded"
    assert event.payload == {
        "channel": "fakechan",
        "interaction_id": _INTERACTION_ID,
        "client_address": "+15550001111",
        "reason": "host_mismatch",
    }
    assert "callback_url" not in event.payload
    assert "evil.example" not in str(event.payload)  # the URL never leaks into the event


async def test_callback_host_match_forwards(wired, monkeypatch):
    # The stored callback host equals the configured public base -> the normal forward.
    _pin_public_base(monkeypatch, "https://host.example")
    store = FakeStore(_entry())  # _CALLBACK_URL is on host.example
    forward_calls = _stub_forward(monkeypatch, httpx.Response(200))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="yes", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.FORWARDED
    assert forward_calls == [SimpleNamespace(callback_url=_CALLBACK_URL, answer="yes", params=None)]
    assert store.released == ["k"]


async def test_callback_pin_fails_closed_when_public_base_unset(wired, monkeypatch):
    # No configured public base -> FAIL-CLOSED (never forward to an unpinnable host):
    # release, discard, and treat as no correlation; the discarded event names the reason.
    _pin_public_base(monkeypatch, None)
    store = FakeStore(_entry(callback_url="https://anything.example/api/interactions/callback/tkt"))
    forward_calls = _stub_forward(monkeypatch, httpx.Response(200))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="yes", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.NO_CORRELATION
    assert forward_calls == []  # nothing forwarded
    assert store.released == ["k"]
    assert len(wired.events) == 1
    assert wired.events[0].topic == inbound_module.CALLBACK_DISCARDED_EVENT_TOPIC
    assert wired.events[0].payload["reason"] == "public_base_unset"


async def test_callback_pin_fails_closed_on_malformed_url(wired, monkeypatch):
    # A stored callback URL with a malformed port makes urlparse().port raise ValueError:
    # FAIL-CLOSED (treat as a mismatch), release, discard, event reason "malformed_url".
    _pin_public_base(monkeypatch, "https://host.example")
    store = FakeStore(_entry(callback_url="https://host.example:notaport/api/interactions/callback/tkt"))
    forward_calls = _stub_forward(monkeypatch, httpx.Response(200))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="yes", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.NO_CORRELATION
    assert forward_calls == []
    assert store.released == ["k"]
    assert len(wired.events) == 1
    assert wired.events[0].payload["reason"] == "malformed_url"


# -- _bridge tolerance parity (old channels ack'd blank/unrouted bridges) --------


@pytest.mark.parametrize("error", [BlankInboundTextError("blank"), LookupError("no route")])
async def test_404_bridge_tolerates_blank_or_unrouted_reply(wired, monkeypatch, error):
    # Parity with the hand-rolled channels: a blank reply or an address with no bound
    # route is nothing to bridge — the ladder acks it (BRIDGED) and never re-raises, so
    # a permanently-blank/unrouted message can't provoke a provider retry-storm.
    async def _raising_accept(*args, **kwargs):
        raise error

    monkeypatch.setattr(app, "_conversation_accept", _raising_accept)
    store = FakeStore(_entry())
    _stub_forward(monkeypatch, httpx.Response(404))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="x", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.BRIDGED  # handled, not rethrown
    assert store.released == ["k"]


# -- security carry-in (b): rejection-reason truncation --------------------------


async def test_400_reason_is_truncated_in_notice_and_event(wired, monkeypatch):
    # A pathological door reason must not blow up the guest notice or the persisted
    # event: the reason is bounded to 300 chars before either use.
    long_reason = "x" * 1000
    store = FakeStore(_entry())
    _stub_forward(monkeypatch, httpx.Response(400, json={"error": long_reason, "retry_in_place": True}))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="maybe", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.RETRY_KEPT
    truncated = "x" * 300
    # The result carries the SAME truncated reason the notice/event use.
    assert result.retry_reason == truncated
    assert len(wired.events) == 1
    assert wired.events[0].payload["reason"] == truncated
    assert len(wired.events[0].payload["reason"]) == 300
    assert len(wired.channel.notifications) == 1
    # The notice carries the truncated reason and nothing of the 1000-char original.
    assert truncated in wired.channel.notifications[0].message
    assert "x" * 301 not in wired.channel.notifications[0].message


async def test_malformed_400_body_defaults_to_retry_in_place(wired, monkeypatch):
    # An unreadable 400 body must never be mistaken for a hard mismatch — it degrades
    # to the safe default (retry-in-place, correlation kept).
    store = FakeStore(_entry())
    _stub_forward(monkeypatch, httpx.Response(400, text="not json at all"))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="x", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.RETRY_KEPT
    assert result.retry_reason is None  # an unreadable body yields no usable reason
    assert store.released == []
    assert len(wired.channel.notifications) == 1
    assert len(wired.events) == 1
    assert wired.events[0].payload["reason"] == ""


# -- params seam symmetry: bridge params ride BOTH arms --------------------------


def _bridge_with_params() -> InboundBridge:
    return InboundBridge(
        channel_id="fakechan",
        our_identity="op-1",
        client_address="+15550001111",
        cap_key="+15550001111",
        provider_message_id="prov-msg-1",
        bridge_text="hello there",
        params={"reply_id": "wamid.X", "referral": "ad-42"},
    )


async def test_bridge_params_forward_to_the_door_alongside_the_answer(wired, monkeypatch):
    # On the FORWARD arm the enrichment params ride to the callback door beside the answer,
    # landing on InteractionResponse.params for the asking flow.
    store = FakeStore(_entry())
    forward_calls = _stub_forward(monkeypatch, httpx.Response(200))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="yes", store=store, bridge=_bridge_with_params()
    )

    assert result.outcome is InboundAnswerOutcome.FORWARDED
    assert forward_calls == [
        SimpleNamespace(callback_url=_CALLBACK_URL, answer="yes", params={"reply_id": "wamid.X", "referral": "ad-42"})
    ]


async def test_bridge_params_pass_to_accept_on_the_bridge_arm(wired, monkeypatch):
    # On the BRIDGE arm (a gone-ask 404) the SAME params reach accept as entry params — the
    # seam symmetry, so enrichment is never dropped on either arm.
    store = FakeStore(_entry())
    _stub_forward(monkeypatch, httpx.Response(404))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="late", store=store, bridge=_bridge_with_params()
    )

    assert result.outcome is InboundAnswerOutcome.BRIDGED
    assert len(wired.accept_calls) == 1
    assert wired.accept_calls[0].params == {"reply_id": "wamid.X", "referral": "ad-42"}


# -- on_mismatch policy: retry (default) vs bridge (digression) at the 400 seam --------


async def test_400_bridge_policy_keeps_correlation_bridges_and_sends_no_notice(wired, monkeypatch):
    from tai42_contract.channels import InboundAnswerOutcome
    from tai42_contract.interactions.models import AnswerMismatchPolicy

    # A bridge-policy ask treats an unmatched reply as a DIGRESSION: the correlation is KEPT
    # (the ask stays parked), NO guest notice is sent, and the reply is handed to the
    # conversation as a fresh routed turn through the STANDARD target-agnostic accept door
    # (a tool route and an agent route are dispatched identically — accept's tool-route
    # dispatch is covered in test_turn.py). The operator event still fires, tagged bridge.
    store = FakeStore(_entry(on_mismatch=AnswerMismatchPolicy.BRIDGE))
    _stub_forward(monkeypatch, httpx.Response(400, json={"error": "not a valid choice", "retry_in_place": True}))

    result = await handle_inbound_answer(
        channel_id="fakechan",
        correlation_key="k",
        answer="tell me about refunds",
        store=store,
        bridge=_bridge_with_params(),
    )

    assert result.outcome is InboundAnswerOutcome.BRIDGED_KEPT
    assert store.released == []  # the ask stays parked — only a real answer or timeout ends it
    assert wired.channel.notifications == []  # no guest notice on the digression
    # The reply entered through the standard accept door, carrying its enrichment params.
    assert len(wired.accept_calls) == 1
    assert wired.accept_calls[0].client_address == "+15550001111"
    assert wired.accept_calls[0].params == {"reply_id": "wamid.X", "referral": "ad-42"}
    # One operator event, tagged with the bridge policy and no guest notice owner.
    assert len(wired.events) == 1
    assert wired.events[0].payload["policy"] == "bridge"
    assert wired.events[0].payload["notice_owner"] == "none"


async def test_400_retry_policy_is_the_default_and_tags_the_event(wired, monkeypatch):
    from tai42_contract.channels import InboundAnswerOutcome

    # A default (retry) ask keeps today's behavior exactly: correlation kept, guest notified,
    # reply NOT bridged, and the operator event carries policy="retry".
    store = FakeStore(_entry())  # default on_mismatch == retry
    _stub_forward(monkeypatch, httpx.Response(400, json={"error": "not a valid choice", "retry_in_place": True}))

    result = await handle_inbound_answer(
        channel_id="fakechan", correlation_key="k", answer="nope", store=store, bridge=_bridge()
    )

    assert result.outcome is InboundAnswerOutcome.RETRY_KEPT
    assert store.released == []
    assert len(wired.channel.notifications) == 1  # the retry notice
    assert wired.accept_calls == []  # a retry never bridges
    assert len(wired.events) == 1
    assert wired.events[0].payload["policy"] == "retry"


# -- mismatch_notice: a per-ask custom retry notice (retry policy ONLY) ----------------


async def test_retry_custom_mismatch_notice_replaces_default_and_fills_reason(wired, monkeypatch):
    # A retry-policy ask with a custom notice REPLACES the built-in text; a literal {reason}
    # token is filled by plain substitution with the door's reason.
    store = FakeStore(_entry(mismatch_notice="Please pick a listed option ({reason})."))
    _stub_forward(monkeypatch, httpx.Response(400, json={"error": "not a valid choice", "retry_in_place": True}))

    await handle_inbound_answer(channel_id="fakechan", correlation_key="k", answer="huh", store=store, bridge=_bridge())

    assert len(wired.channel.notifications) == 1
    assert wired.channel.notifications[0].message == "Please pick a listed option (not a valid choice)."


async def test_retry_custom_mismatch_notice_without_placeholder_is_verbatim(wired, monkeypatch):
    # A custom notice WITHOUT the {reason} token is sent verbatim; stray braces never raise.
    store = FakeStore(_entry(mismatch_notice="That won't work here {oops}."))
    _stub_forward(monkeypatch, httpx.Response(400, json={"error": "bad", "retry_in_place": True}))

    await handle_inbound_answer(channel_id="fakechan", correlation_key="k", answer="huh", store=store, bridge=_bridge())

    assert wired.channel.notifications[0].message == "That won't work here {oops}."


async def test_retry_default_mismatch_notice_unchanged_when_none(wired, monkeypatch):
    # None keeps today's built-in notice (carrying the door reason).
    store = FakeStore(_entry())  # mismatch_notice defaults to None
    door_body = {"error": "Please answer with yes or no.", "retry_in_place": True}
    _stub_forward(monkeypatch, httpx.Response(400, json=door_body))

    await handle_inbound_answer(channel_id="fakechan", correlation_key="k", answer="huh", store=store, bridge=_bridge())

    assert "Please answer with yes or no." in wired.channel.notifications[0].message


async def test_bridge_policy_ignores_mismatch_notice(wired, monkeypatch):
    from tai42_contract.interactions.models import AnswerMismatchPolicy

    # Under bridge policy a custom notice is IGNORED — a digression never notifies.
    store = FakeStore(_entry(on_mismatch=AnswerMismatchPolicy.BRIDGE, mismatch_notice="You should never see this"))
    _stub_forward(monkeypatch, httpx.Response(400, json={"error": "nope", "retry_in_place": True}))

    await handle_inbound_answer(channel_id="fakechan", correlation_key="k", answer="huh", store=store, bridge=_bridge())

    assert wired.channel.notifications == []
