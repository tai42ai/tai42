"""The webhook route and payload traversal — batching (every message and every
status processed), and odd/empty/type-confused envelope shapes acked not 500'd."""

from __future__ import annotations

import pytest
from tai42_contract.channels import AnswerForwardError
from tai42_contract.conversations import DeliveryReceipt

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)

from .conftest import (
    WA_ID,
    FakeHttpx,
    FakeRedis,
    _pending_intact,
    _seed_pending,
    message_payload,
    signed_request,
    status_payload,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")


# --- Batching: every message and every status is processed ---------------------


async def test_batched_messages_all_bridge(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A single value carries two messages; BOTH must bridge (not just messages[0]),
    # or the second is permanently lost after the 200-ack.
    payload = message_payload(wamid="wamid.M1", text="first")
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"].append({"id": "wamid.M2", "from": WA_ID, "type": "text", "text": {"body": "second"}})

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M1", "wamid.M2"]
    assert [c["text"] for c in stub_app.conversations.accept_calls] == ["first", "second"]


async def test_batched_message_lookuperror_continues_to_next(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An unrouted (LookupError) first message is logged+skipped; the second is still
    # attempted (a per-message drop must not abandon the rest of the batch).
    stub_app.conversations.accept_error = LookupError("no channel conversation route matches")
    payload = message_payload(wamid="wamid.M1", text="first")
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"].append({"id": "wamid.M2", "from": WA_ID, "type": "text", "text": {"body": "second"}})

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M1", "wamid.M2"]


async def test_batched_failing_reply_does_not_starve_independent_bridge(
    handler, stub_app, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # m1 is a correlated reply whose callback door persistently 5xx's; m2 is an
    # independent bridge message for a different wa_id. m1's propagating failure
    # must NOT abandon m2: m2 bridges on the first attempt, the batch still 5xx's
    # so the un-acked m1 redelivers, and on redelivery m2 is wamid-skipped (never
    # double-bridged). Reverting to an early raise leaves accept_calls empty here.
    await _seed_pending()  # pending for (PHONE_NUMBER_ID, WA_ID) — m1 correlates
    payload = message_payload(wamid="wamid.M1", text="the answer")
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"].append({"id": "wamid.M2", "from": "15559990002", "type": "text", "text": {"body": "independent"}})
    # m1 correlates (a pending exists) so it reaches the ladder, which raises; m2 has no
    # pending so it never reaches the ladder — it bridges independently.
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: down")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(payload))

    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M2"]  # m2 committed
    assert await _pending_intact(fake_redis)  # m1's ask kept — it retries on redelivery
    assert "channel:whatsapp:seen:wamid.M1" not in fake_redis.store  # m1 un-acked
    assert "channel:whatsapp:seen:wamid.M2" in fake_redis.store  # m2 acked

    # Redelivery: m1 fails again (ladder still raises), m2 is dedupe-skipped (no re-bridge).
    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(payload))

    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M2"]  # still once
    assert len(channels.inbound_calls) == 2  # m1 reached the ladder twice; m2 never (no pending)


async def test_status_infra_failure_reraises_but_later_message_commits(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A status whose record_delivery_status raises a non-LookupError (infra) 5xx's
    # so Meta redelivers — but a message batched in the same value still bridges
    # and commits its dedupe first (no head-of-line starvation on the status path).
    stub_app.conversations.status_error = RuntimeError("delivery store unavailable")
    payload = message_payload(wamid="wamid.MSG", text="hello")
    value = payload["entry"][0]["changes"][0]["value"]
    value["statuses"] = [{"id": "wamid.OUT", "status": "delivered", "recipient_id": WA_ID}]

    with pytest.raises(RuntimeError, match="delivery store unavailable"):
        await handler(signed_request(payload))

    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.MSG"]  # committed
    assert "channel:whatsapp:seen:wamid.MSG" in fake_redis.store


async def test_batched_statuses_two_entries_all_settle(handler, stub_app):
    # Two entries, each carrying one status; BOTH must settle (the status path must
    # iterate every entry/value, not just the first).
    payload = status_payload(wamid="wamid.S1", state="delivered")
    second = status_payload(wamid="wamid.S2", state="failed")
    payload["entry"].append(second["entry"][0])

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert [c["provider_message_id"] for c in stub_app.conversations.status_calls] == ["wamid.S1", "wamid.S2"]
    assert [c["status"] for c in stub_app.conversations.status_calls] == [
        DeliveryReceipt.DELIVERED,
        DeliveryReceipt.FAILED,
    ]


async def test_type_confused_envelope_is_not_5xx(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A well-signed but type-confused envelope (non-object entry/change/metadata,
    # non-list changes, non-object message and status items) must be 200-acked,
    # never AttributeError -> 500.
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            "not-a-dict",
            {"id": "A", "changes": "not-a-list"},
            {
                "id": "B",
                "changes": [
                    "not-a-dict",
                    {
                        "field": "messages",
                        "value": {"metadata": ["nope"], "messages": ["not-a-dict", 42], "statuses": ["not-a-dict"]},
                    },
                ],
            },
        ],
    }

    result = await handler(signed_request(payload))

    assert result.status_code < 500
    assert stub_app.conversations.accept_calls == []
    assert stub_app.conversations.status_calls == []


async def test_dict_payload_with_non_list_entry_is_acked(handler, stub_app):
    # A well-signed object payload whose "entry" is not a list carries nothing to
    # act on — 200-acked, no effect.
    result = await handler(signed_request({"object": "whatsapp_business_account", "entry": "not-a-list"}))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []


async def test_unknown_change_shape_is_acked(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A change carrying neither messages nor statuses (e.g. an account update) is
    # acked with no effect.
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "X", "changes": [{"field": "account_update", "value": {"event": "PARTNER_ADDED"}}]}],
    }
    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []
    assert stub_app.conversations.status_calls == []


async def test_empty_entry_is_acked(handler, stub_app):
    result = await handler(signed_request({"object": "whatsapp_business_account", "entry": []}))

    assert result.status_code == 200


async def test_non_object_json_payload_is_acked(handler, stub_app):
    # A well-signed body that is valid JSON but not an object (e.g. a bare list)
    # carries no entry/changes to act on — acked, no effect.
    result = await handler(signed_request([]))  # type: ignore[arg-type]

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []
