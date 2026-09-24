"""Flow-form replies (``nfm_reply``) — coercion, forward, door-rejection recovery,
and the ask-less ``tai42-nf:`` notify-form namespace."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from tai42_contract.channels import AnswerForwardError, ChannelDeliveryError, InboundAnswerOutcome
from tai42_kit.settings import reset_all_settings

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_whatsapp.channel import WhatsAppChannel
from tai42_channel_whatsapp.correlation import reserve_pending
from tai42_channel_whatsapp.flows import build_form_flow, component_names
from tai42_channel_whatsapp.inbound.forms import (
    _CALLBACK_REJECTION_OPAQUE,
    _FLOW_BODY_MAX_CHARS,
    _FORM_REJECTION_LEAD,
    _FORM_UNPROCESSABLE,
    _MAX_FORM_REJECTIONS,
)

from .conftest import (
    _FORM_QUESTION,
    _FORM_SCHEMA,
    _PENDING_KEY,
    _SEEN_KEY,
    _WAMID,
    PHONE_NUMBER_ID,
    WA_ID,
    FakeHttpx,
    FakeRedis,
    _pending_intact,
    _seed_pending_form,
    form_reply_payload,
    interactive_payload,
    make_delivery,
    message_payload,
    response,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")


# --- Form (Flow) replies: nfm_reply -------------------------------------------

_WABA_ID = "WABA-100"


def _flow_cache_key() -> str:
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    return f"channel:whatsapp:flow:{_WABA_ID}:{schema_hash}"


def _flow_created(flow_id: str = "flow-remade") -> httpx.Response:
    """A Cloud-API create for a re-published Flow (a re-send after the cache was lost)."""
    return response(200, json={"id": flow_id})


def _published() -> httpx.Response:
    """A Cloud-API publish ack."""
    return response(200, json={"success": True})


@pytest.fixture
def waba_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Add the WABA id the form re-send path resolves the cached flow id under."""
    monkeypatch.setenv("CHANNEL_WHATSAPP_WABA_ID", _WABA_ID)
    reset_all_settings()


def _seed_flow_cache(fake_redis: FakeRedis, flow_id: str = "flow-cached") -> None:
    """The published-flow cache entry the original form send left behind."""
    fake_redis.store[_flow_cache_key()] = flow_id


def _set_stored_rejections(fake_redis: FakeRedis, rejections: int) -> None:
    """Rewrite the pending form record's rejection counter in place (drive the cap)."""
    data = json.loads(fake_redis.store[_PENDING_KEY])
    data["rejections"] = rejections
    fake_redis.store[_PENDING_KEY] = json.dumps(data)


def _stored_rejections(fake_redis: FakeRedis) -> int:
    """The pending form record's current rejection counter."""
    return json.loads(fake_redis.store[_PENDING_KEY])["rejections"]


def _flow_accepted(wamid: str = "wamid.RESEND") -> httpx.Response:
    """A Cloud-API accept for a re-sent Flow message."""
    return response(200, json={"messages": [{"id": wamid}]})


async def test_form_reply_forwards_coerced_answer_dict(handler, channels, fake_redis: FakeRedis):
    await _seed_pending_form()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(
        signed_request(
            form_reply_payload({"flow_token": "int-1", "note": "ship it", "qty": "7", "amount": "3.5", "agree": True})
        )
    )

    assert result.status_code == 200
    # flow_token stripped; qty string→int; amount string→float; agree bool passthrough; note string.
    assert channels.inbound_calls[0].answer == {"note": "ship it", "qty": 7, "amount": 3.5, "agree": True}
    # A form ask owns its retry notice — its correction surface is a re-opened Flow.
    assert channels.inbound_calls[0].bridge.owns_retry_notice is True
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert _SEEN_KEY in fake_redis.store


async def test_form_reply_coerces_string_boolean(handler, channels, fake_redis: FakeRedis):
    await _seed_pending_form()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x", "agree": "true"})))

    assert channels.inbound_calls[0].answer == {"note": "x", "agree": True}


async def test_form_reply_bad_coercion_forwards_raw_then_re_sends_flow(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A decimal in an integer field is forwarded raw (int("3.5") fails); the ladder's
    # RETRY_KEPT on this form ask recovers by re-sending a fresh Flow (the channel owns
    # the participant correction here — the ladder sent no notice).
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    # The ladder returns the door's own field-naming reason on RETRY_KEPT.
    channels.inbound_retry_reason = "answer at qty: 3.5 is not an integer"
    channels.inbound_retry_field = "qty"
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x", "qty": "3.5"})))

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == {"note": "x", "qty": "3.5"}  # raw, uncoerced, handed to the ladder
    resend = fake_httpx.calls[0]["json"]  # the ONLY send now — the answer forward is the ladder's
    assert resend["interactive"]["type"] == "flow"
    assert resend["interactive"]["action"]["parameters"]["flow_token"] == "int-1"
    assert resend["interactive"]["action"]["parameters"]["flow_id"] == "flow-cached"
    body = resend["interactive"]["body"]["text"]
    assert _FORM_QUESTION in body  # the question is repeated
    assert "qty" in body  # the door's field-naming reason rides the re-sent Flow body (parity restored)
    assert await _pending_intact(fake_redis)  # kept for the re-submission
    assert _stored_rejections(fake_redis) == 1


@pytest.mark.parametrize("raw_number", ["1e999", "nan", "inf"])
async def test_form_reply_non_finite_number_forwarded_raw(
    raw_number: str, waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A value parsing to inf/nan passes jsonschema yet serializes to null downstream;
    # it is forwarded raw so the door 400s and the reply is recovered by a fresh Flow.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(
        signed_request(form_reply_payload({"flow_token": "int-1", "note": "x", "amount": raw_number}))
    )

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == {"note": "x", "amount": raw_number}  # raw, uncoerced
    assert fake_httpx.calls[0]["json"]["interactive"]["type"] == "flow"  # recovered by a fresh Flow
    assert await _pending_intact(fake_redis)
    assert _stored_rejections(fake_redis) == 1


async def test_form_retry_kept_re_sends_fresh_flow(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # RETRY_KEPT on a form ask recovers by re-sending a fresh Flow for the SAME
    # interaction: same flow_token, the cached flow id, a generic error line, the
    # correlation kept alive with the rejection counted, and a single participant message.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = "note: must not be blank"
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    assert len(fake_httpx.calls) == 1  # the fresh Flow send only — the answer forward is the ladder's
    assert fake_httpx.calls[0]["url"].endswith(f"/{PHONE_NUMBER_ID}/messages")  # the fresh Flow send
    resend = fake_httpx.calls[0]["json"]
    assert resend["interactive"]["type"] == "flow"
    params = resend["interactive"]["action"]["parameters"]
    assert params["flow_token"] == "int-1"  # same interaction — reply matching unchanged
    assert params["flow_id"] == "flow-cached"  # reuses the cached flow id
    # The door's OWN reason rides the body (restored parity), not a generic line.
    assert resend["interactive"]["body"]["text"] == (
        f"{_FORM_QUESTION}\n\n{_FORM_REJECTION_LEAD} note: must not be blank"
    )
    assert _stored_rejections(fake_redis) == 1  # counter incremented
    assert await _pending_intact(fake_redis)  # pending kept
    assert _SEEN_KEY in fake_redis.store


async def test_form_retry_kept_without_reason_uses_opaque_line(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # When the door gave no usable reason (retry_reason None), the re-sent Flow falls
    # back to the fixed opaque line — the participant is never shown an intermediary's content.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = None
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    body = fake_httpx.calls[0]["json"]["interactive"]["body"]["text"]
    assert body == f"{_FORM_QUESTION}\n\n{_FORM_REJECTION_LEAD} {_CALLBACK_REJECTION_OPAQUE}"


async def test_form_rejection_long_question_drops_question_keeps_error(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A question long enough to overflow Meta's 1024-char interactive.body.text cap is
    # dropped WHOLE from the re-sent Flow body (never a mid-string ellipsis): the fresh
    # Flow re-presents the fields, so the body carries only the bounded lead+error tail
    # — which always fits — and the send succeeds with counter/pending unchanged.
    long_question = "Q" * (_FLOW_BODY_MAX_CHARS + 100)
    await _seed_pending_form(question=long_question)
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = "note: must not be blank"
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    body = fake_httpx.calls[0]["json"]["interactive"]["body"]["text"]
    assert body == f"{_FORM_REJECTION_LEAD} note: must not be blank"  # tail only — the question is gone
    assert long_question not in body  # no question
    assert not body.startswith("Q")  # not even a chopped prefix of it
    assert "…" not in body  # no ellipsis
    assert "..." not in body
    assert len(body) <= _FLOW_BODY_MAX_CHARS  # fits the vendor cap
    assert _stored_rejections(fake_redis) == 1  # counter incremented, same as the short-question path
    assert await _pending_intact(fake_redis)  # pending kept
    assert _SEEN_KEY in fake_redis.store


async def test_form_rejection_body_at_cap_boundary_keeps_question(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The cap is inclusive: a body whose length is EXACTLY the cap keeps the question,
    # and one char more would drop it. Pins the <= boundary against the constant.
    error = "note: must not be blank"
    tail = f"{_FORM_REJECTION_LEAD} {error}"
    question = "Q" * (_FLOW_BODY_MAX_CHARS - len(tail) - len("\n\n"))
    await _seed_pending_form(question=question)
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = error
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    body = fake_httpx.calls[0]["json"]["interactive"]["body"]["text"]
    assert body == f"{question}\n\n{tail}"  # question kept at exactly the cap
    assert len(body) == _FLOW_BODY_MAX_CHARS
    assert _stored_rejections(fake_redis) == 1
    assert await _pending_intact(fake_redis)


async def test_form_rejection_cap_stops_re_send_and_bridges(
    waba_env,
    stub_app,
    handler,
    channels,
    fake_redis: FakeRedis,
    fake_httpx: FakeHttpx,
    caplog: pytest.LogCaptureFixture,
):
    # Two RETRY_KEPT rejections under the cap each re-send; the one that reaches the cap
    # does NOT re-send — the participant gets one plain final message, the reservation is
    # released, and a later text bridges normally.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    _set_stored_rejections(fake_redis, _MAX_FORM_REJECTIONS - 2)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT

    # First rejection: under the cap → re-send, counter +1.
    fake_httpx.responses.append(_flow_accepted("wamid.RS1"))
    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"}, wamid="wamid.R1")))
    assert fake_httpx.calls[-1]["json"]["interactive"]["type"] == "flow"
    assert _stored_rejections(fake_redis) == _MAX_FORM_REJECTIONS - 1

    # Second rejection: still under the cap → second re-send, now at the cap.
    fake_httpx.responses.append(_flow_accepted("wamid.RS2"))
    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"}, wamid="wamid.R2")))
    assert fake_httpx.calls[-1]["json"]["interactive"]["type"] == "flow"
    assert _stored_rejections(fake_redis) == _MAX_FORM_REJECTIONS

    # Third rejection: at the cap → NO flow send, one plain final message, released.
    fake_httpx.responses.append(_flow_accepted("wamid.FINAL"))
    with caplog.at_level("ERROR"):
        result = await handler(
            signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"}, wamid="wamid.R3"))
        )

    assert result.status_code == 200
    final = fake_httpx.calls[-1]["json"]
    assert final["type"] == "text"  # a plain message, never another flow
    assert final["text"]["body"] == _FORM_UNPROCESSABLE
    assert not await _pending_intact(fake_redis)  # released, not re-sent
    assert any(f"cap {_MAX_FORM_REJECTIONS}" in record.message for record in caplog.records)

    # A later inbound text now bridges normally — the pending is gone.
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION
    await handler(signed_request(message_payload(wamid="wamid.TXT", text="hello")))
    assert len(stub_app.conversations.accept_calls) == 1


async def test_form_rejection_re_send_failure_raises_and_keeps_pending(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A re-send that itself fails (5xx) propagates out of the webhook so Meta
    # redelivers; the pending is kept UNCHANGED (the ladder held it, no bump) and the
    # wamid is NOT marked seen, so the redelivery re-runs the ladder and re-enters.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    fake_httpx.responses.append(response(500, text="graph down"))  # the re-send itself fails

    with pytest.raises(ChannelDeliveryError, match="HTTP 500"):
        await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert await _pending_intact(fake_redis)  # kept for the redelivery
    assert _stored_rejections(fake_redis) == 0  # a failed re-send does not spend the cap
    assert _SEEN_KEY not in fake_redis.store  # not marked seen — redelivery re-enters


async def test_form_rejection_cache_miss_recreates_flow_and_re_sends(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The published-flow cache is empty at re-send time (the store was lost): the one
    # sender re-creates + re-publishes the Flow from the pending record's inputs and
    # re-sends it — never a stale-lookup raise — and re-caches it under the same key.
    await _seed_pending_form()  # no flow-cache entry seeded
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    fake_httpx.responses.append(_flow_created("flow-remade"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    assert [call["url"] for call in fake_httpx.calls] == [
        f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows",
        "https://graph.facebook.com/v23.0/flow-remade/publish",
        f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages",
    ]
    send = fake_httpx.calls[-1]["json"]
    assert send["interactive"]["type"] == "flow"
    params = send["interactive"]["action"]["parameters"]
    assert params["flow_id"] == "flow-remade"
    assert params["flow_token"] == "int-1"
    assert params["flow_action_payload"]["screen"] == "SCREEN_A"
    assert await _pending_intact(fake_redis)
    assert _stored_rejections(fake_redis) == 1
    assert _SEEN_KEY in fake_redis.store
    # The re-created Flow is now cached under the same key a later hit would reuse.
    assert fake_redis.store[_flow_cache_key()] == "flow-remade"


async def test_form_deliver_then_rejection_reuses_the_same_flow_through_the_real_key(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The real key path end to end (no hand-seeded cache): a form ask is delivered
    # (create + publish + cache + send), then a door rejection re-sends the SAME Flow —
    # same flow id, entry screen, and per-send data — resolved through the same cache key.
    from tai42_contract.interactions.models import FormData, FormOption, FormPage

    schema = {
        "type": "object",
        "properties": {"tier": {"type": "string", "enum": ["g", "s"]}, "note": {"type": "string"}},
    }
    data = FormData(values={"note": "hi"}, options={"tier": [FormOption(value="g", label="Gold")]})
    pages = [FormPage(title="Plan", fields=["tier"]), FormPage(title="Say", fields=["note"])]

    # Deliver: cache miss → create + publish + send.
    fake_httpx.responses.append(_flow_created("flow-e2e"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_flow_accepted("wamid.SENT"))
    await WhatsAppChannel().deliver(
        make_delivery(answer_format="form", schema=schema, data=data, pages=pages, question=_FORM_QUESTION)
    )
    first = fake_httpx.calls[-1]["json"]["interactive"]["action"]["parameters"]

    # Rejection: cache hit → send only, the SAME Flow reproduced from the pending inputs.
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    fake_httpx.responses.append(_flow_accepted("wamid.RESENT"))
    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    resend = fake_httpx.calls[-1]["json"]["interactive"]["action"]["parameters"]
    assert resend["flow_id"] == first["flow_id"] == "flow-e2e"  # same published Flow, no re-create
    assert resend["flow_action_payload"]["screen"] == "SCREEN_A"
    assert resend["flow_action_payload"]["data"] == first["flow_action_payload"]["data"]  # same prefill/options


async def test_form_reply_flow_token_mismatch_bridges_and_keeps_pending(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_pending_form(interaction_id="int-1")

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-999", "note": "x"})))

    assert result.status_code == 200
    assert not fake_httpx.calls  # not forwarded
    assert await _pending_intact(fake_redis)  # the live ask is untouched
    assert len(stub_app.conversations.accept_calls) == 1  # bridged like an uncorrelated interactive


async def test_form_reply_malformed_response_json_bridges(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    await _seed_pending_form()
    interactive = {"type": "nfm_reply", "nfm_reply": {"response_json": "{not json"}}
    payload = interactive_payload(interactive=interactive)

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert not fake_httpx.calls  # nothing forwarded
    assert await _pending_intact(fake_redis)  # the live ask is untouched
    assert len(stub_app.conversations.accept_calls) == 1  # bridged
    assert any("response_json" in record.message for record in caplog.records)


async def test_form_reply_non_object_response_json_bridges(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_pending_form()
    interactive = {"type": "nfm_reply", "nfm_reply": {"response_json": json.dumps(["not", "an", "object"])}}
    payload = interactive_payload(interactive=interactive)

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert await _pending_intact(fake_redis)
    assert len(stub_app.conversations.accept_calls) == 1


async def test_form_reply_missing_response_json_bridges(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An nfm_reply whose nfm_reply object carries no response_json string is bridged.
    await _seed_pending_form()
    interactive = {"type": "nfm_reply", "nfm_reply": {"foo": "bar"}}
    payload = interactive_payload(interactive=interactive)

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert await _pending_intact(fake_redis)
    assert len(stub_app.conversations.accept_calls) == 1


async def test_form_reply_door_5xx_raises_and_keeps_pending(handler, channels, fake_redis: FakeRedis):
    await _seed_pending_form()
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: oops")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert await _pending_intact(fake_redis)  # kept — Meta's retry re-resolves
    assert _SEEN_KEY not in fake_redis.store


# --- Ask-less form (notify) replies: the tai42-nf: token namespace ------------

# Pinned as a LITERAL (not the module constant): this prefix is wire-visible in
# every already-delivered form's token, so changing it orphans those forms.
_NF_PREFIX = "tai42-nf:"


def _nf_token(suffix: str = "cafef00d") -> str:
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    return f"{_NF_PREFIX}{schema_hash}:{suffix}"


def _schema_cache_key() -> str:
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    return f"channel:whatsapp:flow-schema:{_WABA_ID}:{schema_hash}"


def _seed_schema_cache(fake_redis: FakeRedis) -> None:
    """The durable schema+map entry the notify-form send left beside the flow id."""
    names = {c: k for k, c in component_names(_FORM_SCHEMA["properties"]).items()}
    fake_redis.store[_schema_cache_key()] = json.dumps({"schema": _FORM_SCHEMA, "names": names})


async def test_notify_form_reply_accepts_coerced_form_and_rendered_text(
    waba_env, handler, stub_app, channels, fake_redis: FakeRedis
):
    _seed_schema_cache(fake_redis)

    result = await handler(
        signed_request(
            form_reply_payload(
                {"flow_token": _nf_token(), "note": "ship it", "qty": "7", "amount": "3.5", "agree": "true"}
            )
        )
    )

    assert result.status_code == 200
    # No pending ask involved: the reply enters the conversation as a participant message,
    # never the inbound-answer ladder.
    assert channels.inbound_calls == []
    assert stub_app.conversations.accept_calls == [
        {
            "channel": "whatsapp",
            "our_identity": PHONE_NUMBER_ID,
            "client_address": WA_ID,
            "cap_key": WA_ID,
            # The rendered label:value lines every consumer sees — the boolean is JSON
            # (``true``), matching the web channel, never Python's ``True`` repr...
            "text": "note: ship it\nqty: 7\namount: 3.5\nagree: true",
            "provider_message_id": _WAMID,
            "params": None,
            # ...and the structured copy: flow_token stripped, values coerced to the
            # cached schema's types.
            "form": {"note": "ship it", "qty": 7, "amount": 3.5, "agree": True},
            "attachments": None,
            "location": None,
        }
    ]
    assert _SEEN_KEY in fake_redis.store


async def test_notify_form_reply_cache_miss_degrades_to_raw_values(
    waba_env, handler, stub_app, channels, fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    # The schema sidecar is empty (lost store): the reply DEGRADES — raw string values,
    # still accepted — never drops.
    with caplog.at_level(logging.WARNING):
        result = await handler(
            signed_request(form_reply_payload({"flow_token": _nf_token(), "note": "ship it", "qty": "7"}))
        )

    assert result.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    assert call["form"] == {"note": "ship it", "qty": "7"}  # raw: no schema to coerce against
    assert call["text"] == "note: ship it\nqty: 7"
    assert _SEEN_KEY in fake_redis.store
    assert any("forwarding raw" in record.getMessage() for record in caplog.records)


async def test_notify_form_reply_unset_waba_id_degrades_to_raw_values(
    handler, stub_app, channels, fake_redis: FakeRedis
):
    # No CHANNEL_WHATSAPP_WABA_ID: the cache cannot even be addressed — same degrade,
    # never a raise that would have Meta redeliver a permanently-failing reply.
    result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token(), "qty": "7"})))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls[0]["form"] == {"qty": "7"}
    assert _SEEN_KEY in fake_redis.store


async def test_notify_form_reply_leaves_pending_ask_untouched(
    waba_env, handler, stub_app, channels, fake_redis: FakeRedis
):
    # The masquerade fence: a notify-form reply on a pair with a PENDING ask must not
    # answer, claim, or disturb that ask — the token namespace routes it out before any
    # pending peek.
    await _seed_pending_form()
    _seed_schema_cache(fake_redis)
    pending_before = fake_redis.store[_PENDING_KEY]

    result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token(), "note": "unrelated"})))

    assert result.status_code == 200
    assert channels.inbound_calls == []  # the ladder never saw it
    assert fake_redis.store[_PENDING_KEY] == pending_before  # byte-identical record
    assert stub_app.conversations.accept_calls[0]["form"] == {"note": "unrelated"}

    # The ask is still answerable: a genuine (non-prefixed) form reply resolves it.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "the answer"}, wamid="wamid.ASK")))
    assert channels.inbound_calls[0].answer == {"note": "the answer"}


async def test_notify_form_reply_redelivery_deduped(waba_env, handler, stub_app, channels, fake_redis: FakeRedis):
    _seed_schema_cache(fake_redis)
    fake_redis.store[_SEEN_KEY] = "1"

    result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token(), "note": "again"})))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []
    assert channels.inbound_calls == []


async def test_notify_form_reply_empty_form_bridges_compact_json(waba_env, handler, stub_app, fake_redis: FakeRedis):
    # A completed form carrying nothing but its token still needs non-blank text for
    # the accept door: the compact-JSON fallback.
    _seed_schema_cache(fake_redis)

    result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token()})))

    assert result.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    assert call["form"] == {}
    assert call["text"] == "{}"
    assert _SEEN_KEY in fake_redis.store


async def test_notify_form_reply_unrouted_logged_and_acked(
    waba_env, handler, stub_app, fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    _seed_schema_cache(fake_redis)
    stub_app.conversations.accept_error = LookupError("no route for (whatsapp, ...)")

    with caplog.at_level(logging.WARNING):
        result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token(), "note": "x"})))

    assert result.status_code == 200  # dropped and logged, never a 5xx retry loop
    assert _SEEN_KEY in fake_redis.store
    assert any("unrouted" in record.getMessage() for record in caplog.records)


async def test_non_prefixed_form_reply_takes_the_ask_path_unchanged(
    waba_env, handler, stub_app, channels, fake_redis: FakeRedis
):
    # A reply whose token is NOT in the notify namespace runs the ask path: pending peek,
    # ladder resolve, no direct bridge accept.
    await _seed_pending_form()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x", "qty": "7"})))

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == {"note": "x", "qty": 7}
    assert stub_app.conversations.accept_calls == []


# --- Odd (non-identifier) property names: component-name ↔ schema-key mapping --

_ODD_SCHEMA = {
    "type": "object",
    "properties": {"a.b=/c/4:d": {"type": "integer"}, "note": {"type": "string"}},
    "required": [],
}


async def _seed_pending_odd_form(interaction_id: str = "int-1") -> None:
    """A pending form ask whose schema uses a non-identifier property name, with the
    component-name reverse map the deliver path stores."""
    from datetime import UTC, datetime, timedelta

    names = {c: k for k, c in component_names(_ODD_SCHEMA["properties"]).items()}
    await reserve_pending(
        PHONE_NUMBER_ID,
        WA_ID,
        "https://app.example/api/interactions/callback/ticket-1",
        datetime.now(UTC) + timedelta(minutes=5),
        interaction_id=interaction_id,
        schema=_ODD_SCHEMA,
        question="Q?",
        form_names=names,
    )


async def test_form_reply_maps_component_names_back_to_schema_keys(handler, channels, fake_redis: FakeRedis):
    # Meta relays the completed form keyed by the Flow's COMPONENT names; the decode maps
    # every key back to the original schema key before coercion.
    await _seed_pending_odd_form()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    names = component_names(_ODD_SCHEMA["properties"])
    response = {"flow_token": "int-1", names["a.b=/c/4:d"]: "7", names["note"]: "hi"}

    result = await handler(signed_request(form_reply_payload(response)))

    assert result.status_code == 200
    # Keyed by the ORIGINAL schema keys, the integer coerced from its Flow string.
    assert channels.inbound_calls[0].answer == {"a.b=/c/4:d": 7, "note": "hi"}


async def test_form_reply_unknown_component_name_forwarded_under_its_own_name(
    handler, channels, fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    # A response key the map does not know is forwarded under its own name with a warning,
    # never dropped silently.
    await _seed_pending_odd_form()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    names = component_names(_ODD_SCHEMA["properties"])
    response = {"flow_token": "int-1", names["note"]: "hi", "ghost_field": "x"}

    with caplog.at_level(logging.WARNING):
        result = await handler(signed_request(form_reply_payload(response)))

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == {"note": "hi", "ghost_field": "x"}
    assert any("unknown component name" in record.getMessage() for record in caplog.records)


async def test_notify_form_reply_maps_component_names_back_to_schema_keys(
    waba_env, handler, stub_app, channels, fake_redis: FakeRedis
):
    # The ask-less notify path decodes the same way: the sidecar carries the schema and the
    # reverse map, and the reply's component-named keys resolve to schema keys.
    _, schema_hash = build_form_flow(_ODD_SCHEMA)
    names = {c: k for k, c in component_names(_ODD_SCHEMA["properties"]).items()}
    fake_redis.store[f"channel:whatsapp:flow-schema:{_WABA_ID}:{schema_hash}"] = json.dumps(
        {"schema": _ODD_SCHEMA, "names": names}
    )
    token = f"{_NF_PREFIX}{schema_hash}:cafef00d"
    cnames = component_names(_ODD_SCHEMA["properties"])
    response = {"flow_token": token, cnames["a.b=/c/4:d"]: "7", cnames["note"]: "hi"}

    result = await handler(signed_request(form_reply_payload(response)))

    assert result.status_code == 200
    assert channels.inbound_calls == []  # a notify reply is a participant turn, not an answer
    assert stub_app.conversations.accept_calls[0]["form"] == {"a.b=/c/4:d": 7, "note": "hi"}
