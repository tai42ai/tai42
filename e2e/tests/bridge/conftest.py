"""Fixtures for the messaging-bridge suite: the ``BridgeHarness`` handle and a per-spec
reset of the channel stubs + the scripted LLM. The ``bridge_stack`` / ``fake_*`` / ``llm_stub``
fixtures come from the top-level ``tests/conftest.py``.

On a profile that loads twilio and whatsapp, those stubs post a signed ``delivered`` receipt for
every accepted send, as the real media do; a spec that posts a message's status itself withholds
that message's receipt (``receipts.withhold``)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tai42_e2e.channel_stubs import FakeSlack, FakeTelegram, FakeTwilio, FakeWhatsApp, ReceiptDoor
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import TaiStack

from ._bridge_support import TWILIO_STATUS_PATH, WHATSAPP_INBOUND_PATH, BridgeHarness


@pytest.fixture(autouse=True)
def _reset_bridge_stubs(
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
    fake_telegram: FakeTelegram,
    fake_slack: FakeSlack,
    llm_stub: LlmStub,
) -> Iterator[None]:
    """Clear the channel stubs and the LLM script before each spec — combined with a
    per-spec ``uniq`` token, no assertion leans on shared session state — and fail on any
    receipt the deployment refused, before the spec (one that landed after the previous
    spec ended) and after it."""
    _assert_receipts_accepted(fake_twilio, fake_whatsapp, when="before this spec")
    fake_twilio.reset()
    fake_whatsapp.reset()
    fake_telegram.reset()
    fake_slack.reset()
    llm_stub.reset()
    yield
    _assert_receipts_accepted(fake_twilio, fake_whatsapp, when="during this spec")


def _assert_receipts_accepted(fake_twilio: FakeTwilio, fake_whatsapp: FakeWhatsApp, *, when: str) -> None:
    failures = fake_twilio.receipts.drain_failures() + fake_whatsapp.receipts.drain_failures()
    assert not failures, f"the deployment refused automatic delivery receipts posted {when}: {failures}"


def _with_receipts(harness: BridgeHarness) -> BridgeHarness:
    """Point the twilio and whatsapp stubs' automatic receipts at the stack's status doors."""
    origin = harness.stack.origin(harness.stack.port_b)
    harness.fake_twilio.receipts.door = ReceiptDoor(f"{origin}{TWILIO_STATUS_PATH}", harness.twilio_secret)
    harness.fake_whatsapp.receipts.door = ReceiptDoor(f"{origin}{WHATSAPP_INBOUND_PATH}", harness.whatsapp_secret)
    return harness


@pytest.fixture
def bridge(
    bridge_stack: tuple[TaiStack, str],
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
    llm_stub: LlmStub,
) -> BridgeHarness:
    stack, root_token = bridge_stack
    return _with_receipts(BridgeHarness(stack, root_token, fake_twilio, fake_whatsapp, llm_stub))


@pytest.fixture
def media_bridge(
    media_bridge_stack: tuple[TaiStack, str],
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
    fake_telegram: FakeTelegram,
    fake_slack: FakeSlack,
    llm_stub: LlmStub,
) -> BridgeHarness:
    """A ``BridgeHarness`` over the all-four-channels media-bridge profile — the handle the
    inbound-media bridge suite drives (telegram + slack + twilio + whatsapp)."""
    stack, root_token = media_bridge_stack
    return _with_receipts(
        BridgeHarness(
            stack, root_token, fake_twilio, fake_whatsapp, llm_stub, fake_telegram=fake_telegram, fake_slack=fake_slack
        )
    )


@pytest.fixture
def media_bridge_no_store(
    media_bridge_no_store_stack: tuple[TaiStack, str],
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
    fake_telegram: FakeTelegram,
    fake_slack: FakeSlack,
    llm_stub: LlmStub,
) -> BridgeHarness:
    """A ``BridgeHarness`` over the all-four-channels media-bridge profile with NO blob provider —
    the store-unavailable rejection leg (the ingest chokepoint raises ``MediaStoreUnavailableError``)."""
    stack, root_token = media_bridge_no_store_stack
    return _with_receipts(
        BridgeHarness(
            stack, root_token, fake_twilio, fake_whatsapp, llm_stub, fake_telegram=fake_telegram, fake_slack=fake_slack
        )
    )


@pytest.fixture
def web_media(
    web_media_stack: tuple[TaiStack, str],
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
    llm_stub: LlmStub,
) -> BridgeHarness:
    """A ``BridgeHarness`` over the web-media profile — the handle the web inbound-media suite
    drives (the web channel's upload + message doors on a media-capable stack). The twilio/whatsapp
    stubs are carried to satisfy the harness shape; the suite drives web alone."""
    stack, root_token = web_media_stack
    return BridgeHarness(stack, root_token, fake_twilio, fake_whatsapp, llm_stub)


@pytest.fixture
def web_media_expiry(
    web_media_expiry_stack: tuple[TaiStack, str],
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
    llm_stub: LlmStub,
) -> BridgeHarness:
    """A ``BridgeHarness`` over the web-media profile with a SHORT pending-upload TTL — the
    unreferenced-upload expiry leg."""
    stack, root_token = web_media_expiry_stack
    return BridgeHarness(stack, root_token, fake_twilio, fake_whatsapp, llm_stub)


@pytest.fixture
def web_media_no_store(
    web_media_no_store_stack: tuple[TaiStack, str],
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
    llm_stub: LlmStub,
) -> BridgeHarness:
    """A ``BridgeHarness`` over the web-media profile with NO blob provider — the upload
    store-unavailable leg (the ingest chokepoint raises ``MediaStoreUnavailableError``)."""
    stack, root_token = web_media_no_store_stack
    return BridgeHarness(stack, root_token, fake_twilio, fake_whatsapp, llm_stub)


@pytest.fixture
def agent_route_bridge(
    agent_route_park_stack: tuple[TaiStack, str],
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
    llm_stub: LlmStub,
) -> BridgeHarness:
    """A ``BridgeHarness`` over the DURABLE-agent-state bridge profile — the stack a conversation
    AGENT route can actually park on. The twilio/whatsapp stubs are carried only to satisfy the
    harness shape; that profile loads the ``web`` channel alone."""
    stack, root_token = agent_route_park_stack
    return BridgeHarness(stack, root_token, fake_twilio, fake_whatsapp, llm_stub)
