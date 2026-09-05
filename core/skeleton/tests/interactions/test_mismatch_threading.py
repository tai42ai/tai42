"""End-to-end settability of the per-ask ``on_mismatch`` digression policy: the
REAL ``ask_user`` helper threads ``on_mismatch``/``mismatch_notice`` into BOTH the
durable ``InteractionRequest`` and the ``ChannelDelivery`` it builds, and a
bridge-policy ask created through that helper reaches the shared inbound-answer
ladder (which bridges the mismatched reply) once the channel copies the delivery's
policy onto the ``Correlation`` it parks — the seam a real correlated channel uses.

Born-red before the fields were threaded: ``ask_user`` did not accept
``on_mismatch`` at all, so a bridge-policy ask could never be expressed and the
policy could never reach the ladder through this path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, ChannelNotification, Correlation
from tai42_contract.interactions import (
    AnswerMismatchPolicy,
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)

from tai42_skeleton.app.instance import app
from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.channels import inbound as inbound_module
from tai42_skeleton.channels.inbound import InboundAnswerOutcome, InboundBridge, handle_inbound_answer
from tai42_skeleton.hooks import cache as hooks_cache
from tai42_skeleton.interactions import InteractionStore, ask_user
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.settings import InteractionsSettings

_PUBLIC_BASE = "https://host.example"


class CapturingChannel:
    """Records the ``ChannelDelivery`` the helper builds; on delivery it parks a
    ``Correlation`` copying the delivery's mismatch policy onto a shared store — the
    exact copy a real correlated channel performs so its ladder read is authoritative."""

    def __init__(self, store: FakeCorrelationStore) -> None:
        self.deliveries: list[ChannelDelivery] = []
        self._store = store

    async def deliver(self, delivery: ChannelDelivery) -> None:
        self.deliveries.append(delivery)
        await self._store.set_correlation(
            "guest-key",
            Correlation(
                callback_url=delivery.callback_url,
                interaction_id=delivery.interaction_id,
                ttl_deadline=delivery.timeout_at,
                on_mismatch=delivery.on_mismatch,
                mismatch_notice=delivery.mismatch_notice,
            ),
            ttl_seconds=300,
        )

    async def notify(self, notification: ChannelNotification) -> list[str]:  # pragma: no cover - unused
        return []


class FakeCorrelationStore:
    """A ``CorrelationStore`` over one in-memory record; records releases."""

    def __init__(self) -> None:
        self._entry: Correlation | None = None
        self.released: list[str] = []

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        self._entry = entry
        return True

    async def get_correlation(self, key: str) -> Correlation | None:
        return self._entry

    async def release_correlation(self, key: str) -> None:
        self.released.append(key)


class FakeHooksManager:
    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    async def on_event(self, topic, payload, *, tool_kwargs_override=None):
        self.events.append(SimpleNamespace(topic=topic, payload=payload))


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def driver():
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    yield
    reset_execution_identity(id_token)
    reset_resume_continuation_tool(tool_token)


@pytest.fixture
def wired(monkeypatch, fake_client_ctx):
    """Bind the app + a capturing channel over a shared correlation store, and wire the
    helper's settings/redis and the ladder's forward/accept/hooks/public-base seams."""
    app._channel_registry.reset()
    settings = InteractionsSettings(public_base_url=_PUBLIC_BASE)
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    # The ladder pins the callback host to the SAME public base the helper minted from.
    ladder_settings = InteractionsSettings(public_base_url=_PUBLIC_BASE)
    monkeypatch.setattr(inbound_module, "interactions_settings", lambda: ladder_settings)

    corr_store = FakeCorrelationStore()
    channel = CapturingChannel(corr_store)

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
        accept_calls.append(SimpleNamespace(client_address=client_address, text=text))
        return "bridged-msg-1"

    monkeypatch.setattr(app, "_conversation_accept", _fake_accept)
    hooks = FakeHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)

    with tai42_app.bound(app):
        tai42_app.channels.register("cap", channel)
        yield SimpleNamespace(
            channel=channel, corr_store=corr_store, settings=settings, accept_calls=accept_calls, events=hooks.events
        )
    app._channel_registry.reset()


def _stub_forward(monkeypatch, response: httpx.Response):
    async def _fake(callback_url, answer, params=None):
        return response

    monkeypatch.setattr(inbound_module, "_forward_answer", _fake)


async def test_helper_threads_on_mismatch_into_delivery_and_request(wired, fake_redis, driver):
    # The real helper builds a channel-delivered ask with the bridge policy + a custom
    # notice; both fields land on the ChannelDelivery AND the durable InteractionRequest.
    result = await ask_user(
        "proceed?",
        channel="cap",
        recipient="+15550001111",
        on_mismatch=AnswerMismatchPolicy.BRIDGE,
        mismatch_notice="Please answer yes or no ({reason}).",
        mode="async",
        expiry_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert isinstance(result, SuspendedInteraction)

    assert len(wired.channel.deliveries) == 1
    delivery = wired.channel.deliveries[0]
    assert delivery.on_mismatch is AnswerMismatchPolicy.BRIDGE
    assert delivery.mismatch_notice == "Please answer yes or no ({reason})."

    # The durable record carries the same policy for attribution.
    store = InteractionStore(wired.settings.key_prefix)
    state = await store.get_state(fake_redis, delivery.interaction_id)
    assert state is not None
    assert state.request.on_mismatch is AnswerMismatchPolicy.BRIDGE
    assert state.request.mismatch_notice == "Please answer yes or no ({reason})."


async def test_helper_default_on_mismatch_is_retry_on_both_frames(wired, fake_redis, driver):
    # An ask that does not set the policy keeps today's behavior exactly: RETRY on both
    # the delivery and the durable record, and no custom notice.
    result = await ask_user(
        "proceed?",
        channel="cap",
        recipient="+15550001111",
        mode="async",
        expiry_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert isinstance(result, SuspendedInteraction)

    delivery = wired.channel.deliveries[0]
    assert delivery.on_mismatch is AnswerMismatchPolicy.RETRY
    assert delivery.mismatch_notice is None

    store = InteractionStore(wired.settings.key_prefix)
    state = await store.get_state(fake_redis, delivery.interaction_id)
    assert state is not None
    assert state.request.on_mismatch is AnswerMismatchPolicy.RETRY
    assert state.request.mismatch_notice is None


async def test_bridge_policy_reaches_the_ladder_through_the_real_helper(wired, fake_redis, driver, monkeypatch):
    # END TO END: the ask is created via the REAL helper with on_mismatch="bridge"; the
    # channel copies the delivery's policy onto the Correlation it parks; a mismatched
    # guest reply (door 400 on a live ask) then BRIDGES as a digression through the shared
    # ladder — the ask stays parked and no guest notice is sent. This is the settability
    # the field previously lacked: today's helper could not express the policy at all.
    result = await ask_user(
        "proceed?",
        channel="cap",
        recipient="+15550001111",
        on_mismatch=AnswerMismatchPolicy.BRIDGE,
        mode="async",
        expiry_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert isinstance(result, SuspendedInteraction)
    # The parked Correlation carries the bridge policy the helper set on the delivery.
    entry = await wired.corr_store.get_correlation("guest-key")
    assert entry is not None
    assert entry.on_mismatch is AnswerMismatchPolicy.BRIDGE

    _stub_forward(monkeypatch, httpx.Response(400, json={"error": "not a valid choice", "retry_in_place": True}))
    bridge = InboundBridge(
        channel_id="cap",
        our_identity="op-1",
        client_address="+15550001111",
        cap_key="+15550001111",
        provider_message_id="prov-msg-1",
        bridge_text="tell me about refunds",
        owns_retry_notice=False,
    )

    ladder = await handle_inbound_answer(
        channel_id="cap",
        correlation_key="guest-key",
        answer="tell me about refunds",
        store=wired.corr_store,
        bridge=bridge,
    )

    assert ladder.outcome is InboundAnswerOutcome.BRIDGED_KEPT
    assert wired.corr_store.released == []  # the ask stays parked
    assert len(wired.accept_calls) == 1  # the digression was bridged as a fresh turn
    assert wired.channel.deliveries  # the delivery happened through the real helper
    # The operator event is tagged with the bridge policy.
    assert wired.events
    assert wired.events[-1].payload["policy"] == "bridge"
