"""Fixtures for the messaging-bridge suite: the ``BridgeHarness`` handle and a per-spec
reset of the channel stubs + the scripted LLM. The ``bridge_stack`` / ``fake_*`` / ``llm_stub``
fixtures come from the top-level ``tests/conftest.py``."""

from __future__ import annotations

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.netfixtures import FakeTwilio, FakeWhatsApp
from tai42_e2e.stack import TaiStack

from ._bridge_support import BridgeHarness


@pytest.fixture(autouse=True)
def _reset_bridge_stubs(fake_twilio: FakeTwilio, fake_whatsapp: FakeWhatsApp, llm_stub: LlmStub) -> None:
    """Clear the channel stubs and the LLM script before each spec — combined with a
    per-spec ``uniq`` token, no assertion leans on shared session state."""
    fake_twilio.reset()
    fake_whatsapp.reset()
    llm_stub.reset()


@pytest.fixture
def bridge(
    bridge_stack: tuple[TaiStack, str],
    fake_twilio: FakeTwilio,
    fake_whatsapp: FakeWhatsApp,
    llm_stub: LlmStub,
) -> BridgeHarness:
    stack, root_token = bridge_stack
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
