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
