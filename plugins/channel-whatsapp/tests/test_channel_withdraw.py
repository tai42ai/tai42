"""``WhatsAppChannel.withdraw`` — the platform took a delivered question down: release the pair's hold."""

from __future__ import annotations

import json

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from tai42_contract.channels import ChannelDeliveryError, ChannelWithdrawal

from tai42_channel_whatsapp.channel import WhatsAppChannel
from tai42_channel_whatsapp.correlation import (
    EndpointForm,
    PendingQuestionExistsError,
    cache_endpoint_form,
    get_endpoint_form,
    reserve_pending,
)
from tai42_channel_whatsapp.flows import build_form_flow

from .conftest import (
    ALLOWED_A,
    PHONE_NUMBER_ID,
    FakeHttpx,
    FakeRedis,
    _accepted,
    expected_flow_identity,
    make_delivery,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_WABA_ID = "WABA-100"
_FORM_SCHEMA = {
    "type": "object",
    "properties": {"note": {"type": "string"}, "qty": {"type": "integer"}},
    "required": ["note"],
}
_PAIR_KEY = f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}"


@pytest.fixture
def waba_env(monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_WABA_ID", _WABA_ID)
    reset_all_settings()
    # The form's Flow is already published, so each delivery is the send alone.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[f"channel:whatsapp:flow:{_WABA_ID}:{expected_flow_identity(schema_hash)}"] = "flow-cached"


def _form_delivery(interaction_id: str):
    return make_delivery(
        interaction_id=interaction_id, answer_format="form", schema=_FORM_SCHEMA, question="Please fill this in."
    )


def _held_by(fake_redis: FakeRedis) -> str | None:
    raw = fake_redis.store.get(_PAIR_KEY)
    return None if raw is None else json.loads(raw)["interaction_id"]


async def _reserve(interaction_id: str) -> None:
    delivery = make_delivery(interaction_id=interaction_id)
    await reserve_pending(
        PHONE_NUMBER_ID, ALLOWED_A, delivery.callback_url, delivery.timeout_at, interaction_id=interaction_id
    )


async def test_withdraw_frees_the_pair_so_a_second_form_is_accepted(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    channel = WhatsAppChannel()
    fake_httpx.responses.append(_accepted("wamid.A"))
    await channel.deliver(_form_delivery("int-A"))
    assert _held_by(fake_redis) == "int-A"
    # While A holds the pair, a second form to the same person is refused.
    with pytest.raises(PendingQuestionExistsError):
        await channel.deliver(_form_delivery("int-B"))

    await channel.withdraw(ChannelWithdrawal(interaction_id="int-A", recipient=ALLOWED_A))

    fake_httpx.responses.append(_accepted("wamid.B"))
    await channel.deliver(_form_delivery("int-B"))
    assert _held_by(fake_redis) == "int-B"
    sends = [c for c in fake_httpx.calls if c["url"].endswith("/messages")]
    assert [c["json"]["interactive"]["action"]["parameters"]["flow_token"] for c in sends] == ["int-A", "int-B"]


async def test_withdraw_never_drops_another_asks_reservation(fake_redis: FakeRedis):
    await _reserve("int-A")

    await WhatsAppChannel().withdraw(ChannelWithdrawal(interaction_id="int-X", recipient=ALLOWED_A))

    assert _held_by(fake_redis) == "int-A"


async def test_withdraw_is_a_no_op_when_nothing_is_pending(fake_redis: FakeRedis):
    await WhatsAppChannel().withdraw(ChannelWithdrawal(interaction_id="int-A", recipient=ALLOWED_A))
    # A second withdrawal of the same ask is the same no-op.
    await WhatsAppChannel().withdraw(ChannelWithdrawal(interaction_id="int-A", recipient=ALLOWED_A))

    assert fake_redis.store == {}


async def test_withdraw_without_a_recipient_touches_nothing(fake_redis: FakeRedis):
    await _reserve("int-A")
    before = dict(fake_redis.store)
    events_before = list(fake_redis.events)

    await WhatsAppChannel().withdraw(ChannelWithdrawal(interaction_id="int-A"))

    # ``deliver`` refuses a missing recipient before any reservation, so there is nothing to release.
    assert fake_redis.store == before
    assert fake_redis.events == events_before


async def test_withdraw_drops_the_endpoint_form_sidecar(fake_redis: FakeRedis):
    delivery = make_delivery(interaction_id="int-A")
    await _reserve("int-A")
    form = EndpointForm(schema={"type": "object"}, pages=None, reactions=None, option_fields=[], invalid={})
    await cache_endpoint_form("int-A", form, delivery.timeout_at)
    assert await get_endpoint_form("int-A") is not None

    await WhatsAppChannel().withdraw(ChannelWithdrawal(interaction_id="int-A", recipient=ALLOWED_A))

    assert await get_endpoint_form("int-A") is None
    assert _held_by(fake_redis) is None


async def test_withdraw_store_fault_raises_a_retryable_delivery_error(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    async def _down(*_args: object, **_kwargs: object) -> int:
        raise RedisConnectionError("connection refused")

    monkeypatch.setattr(fake_redis, "eval", _down)

    with pytest.raises(ChannelDeliveryError) as excinfo:
        await WhatsAppChannel().withdraw(ChannelWithdrawal(interaction_id="int-A", recipient=ALLOWED_A))
    assert excinfo.value.retryable is True
