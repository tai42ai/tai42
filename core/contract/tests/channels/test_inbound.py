"""Tests for the inbound-answer ladder result shapes: ``InboundBridge`` and ``InboundAnswerResult``."""

from __future__ import annotations

from typing import Any

import pytest


def _inbound_bridge_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "channel_id": "telegram",
        "our_identity": "op-1",
        "client_address": "+15550001111",
        "cap_key": "+15550001111",
        "provider_message_id": "prov-1",
        "bridge_text": "hello",
    }
    base.update(overrides)
    return base


def test_inbound_bridge_owns_retry_notice_defaults_false():
    from tai42_contract.channels import InboundBridge

    bridge = InboundBridge(**_inbound_bridge_kwargs())
    assert bridge.owns_retry_notice is False  # additive default keeps text channels untouched
    assert InboundBridge(**_inbound_bridge_kwargs(owns_retry_notice=True)).owns_retry_notice is True


def test_inbound_bridge_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.channels import InboundBridge

    bridge = InboundBridge(**_inbound_bridge_kwargs())
    with pytest.raises(ValidationError):
        bridge.bridge_text = "changed"


def test_inbound_answer_result_defaults_and_frozen():
    from pydantic import ValidationError

    from tai42_contract.channels import InboundAnswerOutcome, InboundAnswerResult

    result = InboundAnswerResult(outcome=InboundAnswerOutcome.FORWARDED)
    assert result.retry_reason is None
    assert result.retry_field is None
    with pytest.raises(ValidationError):
        result.retry_reason = "changed"


def test_inbound_answer_result_carries_reason_and_field():
    from tai42_contract.channels import InboundAnswerOutcome, InboundAnswerResult

    result = InboundAnswerResult(
        outcome=InboundAnswerOutcome.RETRY_KEPT, retry_reason="pick a listed option", retry_field="choice"
    )
    assert result.outcome is InboundAnswerOutcome.RETRY_KEPT
    assert result.retry_reason == "pick a listed option"
    assert result.retry_field == "choice"


def test_inbound_answer_types_exported():
    import tai42_contract.channels as channels_module

    for name in ("InboundBridge", "InboundAnswerOutcome", "InboundAnswerResult", "AnswerForwardError"):
        assert name in channels_module.__all__
