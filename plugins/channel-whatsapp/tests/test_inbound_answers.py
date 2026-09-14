"""The correlated-answer ladder outcomes and the faithful bridge-text rendering
of a correlated reply (text / select tap / completed form)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tai42_contract.channels import AnswerForwardError, InboundAnswerOutcome

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_whatsapp.correlation import PendingQuestion
from tai42_channel_whatsapp.inbound.answers import _render_answer_for_bridge

from .conftest import (
    _CALLBACK,
    _SEEN_KEY,
    _WAMID,
    FakeRedis,
    _pending_intact,
    _seed_pending,
    _seed_pending_form,
    _seed_pending_select,
    form_reply_payload,
    interactive_payload,
    message_payload,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")


# --- Forward status policy (correlated reply) ---------------------------------


async def test_ladder_forward_error_raises_and_does_not_dedupe(handler, channels, fake_redis: FakeRedis):
    # A door 5xx / 401 / transport fault surfaces as AnswerForwardError from the ladder
    # (which keeps the correlation for the retry); the plugin lets it propagate and does
    # NOT mark the wamid seen, so Meta's redelivery re-runs the ladder.
    await _seed_pending()
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: oops")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(message_payload()))

    assert await _pending_intact(fake_redis)  # the ladder kept it (stub left it intact)
    assert _SEEN_KEY not in fake_redis.store  # NOT marked seen — the retry must not dedupe away


async def test_ladder_bridged_text_reply_acks_and_renders_bridge_text(handler, channels, fake_redis: FakeRedis):
    # A BRIDGED outcome (the ask is gone / a hard mismatch — the ladder already bridged
    # the reply internally, carrying the text under the wamid dedupe key) acks 200 and
    # marks the wamid seen. The channel supplies the faithful bridge text.
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    result = await handler(signed_request(message_payload(text="yes please")))

    assert result.status_code == 200
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert _SEEN_KEY in fake_redis.store
    assert channels.inbound_calls[0].bridge.bridge_text == "yes please"
    assert channels.inbound_calls[0].bridge.provider_message_id == _WAMID

    # Meta redelivers the same wamid: dedupe short-circuits — the ladder is not consulted again.
    redelivery = await handler(signed_request(message_payload(text="yes please")))
    assert redelivery.status_code == 200
    assert len(channels.inbound_calls) == 1  # still once


async def test_bridged_select_tap_renders_the_option_label(handler, channels, fake_redis: FakeRedis):
    # A select tap resolves to its option label in the channel, which is what the
    # ladder's bridge would carry when the interaction is terminally gone.
    await _seed_pending_select(options=["staging", "production"])
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    result = await handler(signed_request(interactive_payload(reply_id="int-1:1", title="production")))

    assert result.status_code == 200
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert channels.inbound_calls[0].answer == "production"  # the tap resolved to options[1]
    assert channels.inbound_calls[0].bridge.bridge_text == "production"
    assert channels.inbound_calls[0].bridge.provider_message_id == _WAMID
    assert _SEEN_KEY in fake_redis.store


async def test_bridged_form_renders_readable_fields(handler, channels, fake_redis: FakeRedis):
    # A completed Flow form's bridge text renders the submitted field/value pairs
    # readably (never a raw JSON dump), which the ladder would carry on a gone ask.
    await _seed_pending_form()
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "ship it", "qty": "7"})))

    assert result.status_code == 200
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert channels.inbound_calls[0].answer == {"note": "ship it", "qty": 7}  # coerced dict forwarded
    bridged = channels.inbound_calls[0].bridge.bridge_text
    assert "note: ship it" in bridged
    assert "qty: 7" in bridged  # coerced to int, rendered as a labelled field line
    assert "{" not in bridged  # not a raw JSON dump
    assert channels.inbound_calls[0].bridge.provider_message_id == _WAMID


def test_render_empty_form_answer_bridges_a_non_empty_string():
    # A completed-but-empty Flow form renders no field lines; the fallback is a faithful
    # compact dump so the bridge is handed a non-blank string (the accept door rejects
    # blank text) and the reply is never dropped.
    pending = PendingQuestion(callback_url=_CALLBACK, timeout_at=datetime.now(UTC), schema={"properties": {}})
    rendered = _render_answer_for_bridge({}, pending)
    assert rendered != ""
    assert rendered == "{}"


def test_render_form_answer_with_a_nested_value_uses_json_not_repr():
    # A non-scalar field value renders as compact JSON, not a Python ``repr``; a scalar
    # field is unchanged.
    pending = PendingQuestion(
        callback_url=_CALLBACK,
        timeout_at=datetime.now(UTC),
        schema={"properties": {"tags": {"title": "Tags"}, "note": {"title": "Note"}}},
    )
    rendered = _render_answer_for_bridge({"tags": ["a", "b"], "note": "ship it"}, pending)
    assert 'Tags: ["a", "b"]' in rendered
    assert "['a', 'b']" not in rendered  # never a Python repr
    assert "Note: ship it" in rendered  # scalar rendering unchanged


def test_render_form_answer_with_a_boolean_renders_json_not_python_repr():
    # A boolean renders through JSON (``true``/``false``) — the same standard-faithful
    # convention the web channel uses — never Python's ``True``/``False`` repr, even
    # though ``bool`` is a subclass of ``int``. The source value stays a real bool: the
    # structured form carried alongside the text is untouched by rendering.
    pending = PendingQuestion(
        callback_url=_CALLBACK,
        timeout_at=datetime.now(UTC),
        schema={"properties": {"subscribed": {"title": "Subscribed"}, "declined": {"title": "Declined"}}},
    )
    answer = {"subscribed": True, "declined": False}
    rendered = _render_answer_for_bridge(answer, pending)
    assert "Subscribed: true" in rendered
    assert "Declined: false" in rendered
    assert "True" not in rendered  # never a Python repr
    assert "False" not in rendered
    assert answer["subscribed"] is True  # unmutated real bools
    assert answer["declined"] is False


async def test_ladder_forward_error_does_not_bridge(handler, stub_app, channels, fake_redis: FakeRedis):
    # A raised AnswerForwardError (5xx/transport) must NOT be converted into a bridge:
    # the ladder kept the correlation and the plugin re-raises for Meta's retry.
    await _seed_pending()
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: oops")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(message_payload()))

    assert stub_app.conversations.accept_calls == []  # never bridged
    assert await _pending_intact(fake_redis)  # kept for the retry


async def test_text_ask_retry_kept_acks_and_keeps_correlation(handler, channels, fake_redis: FakeRedis):
    # RETRY_KEPT on a text ask (owns_retry_notice=False, so the CORE sent the participant
    # notice): the correlation is kept and the wamid is marked seen — redelivering the
    # same body would be rejected again.
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT

    result = await handler(signed_request(message_payload()))

    assert result.status_code == 200
    assert channels.inbound_calls[0].bridge.owns_retry_notice is False  # core owns the text-ask notice
    assert await _pending_intact(fake_redis)  # kept — the human can reply again
    assert _SEEN_KEY in fake_redis.store
