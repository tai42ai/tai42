"""The WhatsApp ``withdraw`` member: free the single pending slot a torn-down ask held.

The platform fires ``channel.withdraw`` when a channel-delivered ask is cancelled or its
thread/person erased; it must release the pair reservation so the NEXT ask to that pair is accepted
at once (PF-FORMS-14), compare by interaction id so a newer ask is never dropped, drop the reacting-
form sidecar, be a no-op when nothing is held, and surface a store fault as a retryable error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelWithdrawal

from tai42_channel_whatsapp.channel.adapter import WhatsAppChannel
from tai42_channel_whatsapp.correlation import (
    PendingQuestionExistsError,
    cache_reaction_form,
    get_reaction_form,
    peek_pending,
    reserve_pending,
)

from .conftest import FakeRedis

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_PNID = "10000000000001"
_WA = "15559990001"
_CALLBACK = "https://app.example/api/interactions/callback/ticket-1"
_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def _deadline(seconds: float = 300) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def _reserve_form(interaction_id: str) -> None:
    await reserve_pending(
        _PNID, _WA, _CALLBACK, _deadline(), interaction_id=interaction_id, schema=_SCHEMA, question="Q?"
    )


async def test_withdraw_frees_the_pair_so_a_second_form_is_accepted(fake_redis: FakeRedis):
    channel = WhatsAppChannel()
    # Form ask A holds the single slot for the pair.
    await _reserve_form("A")
    # A second form to the SAME pair would be refused while A is held.
    with pytest.raises(PendingQuestionExistsError):
        await _reserve_form("B")

    # The platform withdraws A (a cancel/erase) → the slot frees at once.
    await channel.withdraw(ChannelWithdrawal(interaction_id="A", recipient=_WA))

    # Now form B is accepted, and the pending record names B.
    await _reserve_form("B")
    held = await peek_pending(_PNID, _WA)
    assert held is not None
    assert held.interaction_id == "B"


async def test_withdraw_never_drops_another_asks_reservation(fake_redis: FakeRedis):
    channel = WhatsAppChannel()
    # The pair is held for A; a withdraw naming a DIFFERENT interaction must leave A untouched.
    await _reserve_form("A")
    await channel.withdraw(ChannelWithdrawal(interaction_id="X", recipient=_WA))
    held = await peek_pending(_PNID, _WA)
    assert held is not None
    assert held.interaction_id == "A"


async def test_withdraw_is_a_no_op_when_nothing_is_pending(fake_redis: FakeRedis):
    channel = WhatsAppChannel()
    # Nothing reserved (expired/forwarded/never reserved): a clean no-op, no raise.
    await channel.withdraw(ChannelWithdrawal(interaction_id="A", recipient=_WA))
    assert await peek_pending(_PNID, _WA) is None


async def test_withdraw_is_a_no_op_without_a_recipient(fake_redis: FakeRedis):
    channel = WhatsAppChannel()
    # No recipient means the delivery refused before reserving (WhatsApp has no default), so
    # nothing can be held: a no-op that touches no reservation.
    await _reserve_form("A")
    await channel.withdraw(ChannelWithdrawal(interaction_id="A"))
    held = await peek_pending(_PNID, _WA)
    assert held is not None
    assert held.interaction_id == "A"


async def test_withdraw_drops_the_reacting_form_sidecar(fake_redis: FakeRedis):
    channel = WhatsAppChannel()
    await _reserve_form("A")
    await cache_reaction_form("A", _SCHEMA, None, {}, {}, _deadline())
    assert await get_reaction_form("A") is not None

    await channel.withdraw(ChannelWithdrawal(interaction_id="A", recipient=_WA))

    # The sidecar is keyed by the interaction id, so it is dropped unconditionally — the data
    # endpoint stops serving a withdrawn reacting form.
    assert await get_reaction_form("A") is None


async def test_withdraw_store_fault_raises_a_retryable_delivery_error(fake_redis: FakeRedis, monkeypatch):
    channel = WhatsAppChannel()
    await _reserve_form("A")

    class _BoomPipe:
        async def __aenter__(self) -> _BoomPipe:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def watch(self, *keys: str) -> bool:
            raise RuntimeError("redis is down")

    monkeypatch.setattr(fake_redis, "pipeline", lambda: _BoomPipe())

    with pytest.raises(ChannelDeliveryError) as excinfo:
        await channel.withdraw(ChannelWithdrawal(interaction_id="A", recipient=_WA))
    assert excinfo.value.retryable is True
