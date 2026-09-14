"""Tests for ``DeliveryReceipt`` — the terminal fate of an outbound message."""

from __future__ import annotations


def test_delivery_receipt_has_the_two_terminal_outcomes():
    from tai42_contract.conversations import DeliveryReceipt

    assert {m.value for m in DeliveryReceipt} == {"delivered", "failed"}
    assert DeliveryReceipt.DELIVERED == "delivered"
