"""Delivery-status webhooks — receipts recorded against outbound messages, notify_user-send
receipts, and benign handling of untracked ids / odd status shapes."""

from __future__ import annotations

import pytest
from tai42_contract.conversations import DeliveryReceipt

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)

from .conftest import signed_request, status_payload

pytestmark = pytest.mark.usefixtures("whatsapp_env")


@pytest.mark.parametrize("state", ["sent", "delivered"])
async def test_status_sent_or_delivered_records_delivered(handler, stub_app, state: str):
    result = await handler(signed_request(status_payload(wamid="wamid.OUT", state=state)))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == [
        {"channel": "whatsapp", "provider_message_id": "wamid.OUT", "status": DeliveryReceipt.DELIVERED}
    ]


async def test_status_failed_records_failed_loudly(handler, stub_app, caplog: pytest.LogCaptureFixture):
    with caplog.at_level("WARNING"):
        result = await handler(signed_request(status_payload(wamid="wamid.OUT", state="failed")))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == [
        {"channel": "whatsapp", "provider_message_id": "wamid.OUT", "status": DeliveryReceipt.FAILED}
    ]
    assert any("delivery failure" in record.message for record in caplog.records)


async def test_status_read_is_ignored(handler, stub_app):
    result = await handler(signed_request(status_payload(wamid="wamid.OUT", state="read")))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == []  # informational — nothing recorded


async def test_status_unknown_state_is_ignored(handler, stub_app):
    result = await handler(signed_request(status_payload(wamid="wamid.OUT", state="deleted")))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == []


async def test_status_unknown_wamid_is_benign_200(handler, stub_app, caplog: pytest.LogCaptureFixture):
    # record_delivery_status raises LookupError for a wamid the bridge does not
    # track; the route acks rather than 5xx-ing the provider for a message we do
    # not own.
    stub_app.conversations.status_error = LookupError("outbound id wamid.OUT maps to no answer record")

    with caplog.at_level("INFO"):
        result = await handler(signed_request(status_payload(wamid="wamid.OUT", state="delivered")))

    assert result.status_code == 200
    assert any("untracked message wamid.OUT" in record.message for record in caplog.records)


async def test_status_send_hit_delivered_posts_receipt_no_untracked_log(
    handler, stub_app, caplog: pytest.LogCaptureFixture
):
    # The bridge does not own the id (LookupError), but it is a notify_user send: the
    # send-receipt seam resolves it and posts the receipt onto the trace, so NO untracked log.
    stub_app.conversations.status_error = LookupError("outbound id wamid.SEND maps to no answer record")
    stub_app.channels.send_receipt_result = True

    with caplog.at_level("INFO"):
        result = await handler(signed_request(status_payload(wamid="wamid.SEND", state="delivered")))

    assert result.status_code == 200
    assert stub_app.channels.send_receipt_calls == [
        {
            "channel": "whatsapp",
            "provider_message_id": "wamid.SEND",
            "status": DeliveryReceipt.DELIVERED,
            "errors": None,
        }
    ]
    assert not any("untracked message wamid.SEND" in record.message for record in caplog.records)


async def test_status_send_hit_failed_forwards_errors(handler, stub_app):
    stub_app.conversations.status_error = LookupError("outbound id wamid.SEND maps to no answer record")
    stub_app.channels.send_receipt_result = True

    result = await handler(signed_request(status_payload(wamid="wamid.SEND", state="failed")))

    assert result.status_code == 200
    (call,) = stub_app.channels.send_receipt_calls
    assert call["status"] is DeliveryReceipt.FAILED
    # The provider's error detail rides the receipt so the trace event carries it.
    assert call["errors"] == [{"code": 131047, "title": "Re-engagement message"}]


async def test_status_bad_signature_is_401(handler, stub_app):
    result = await handler(signed_request(status_payload(), secret="other-secret"))

    assert result.status_code == 401
    assert stub_app.conversations.status_calls == []


async def test_status_entry_missing_fields_is_skipped(handler, stub_app, caplog: pytest.LogCaptureFixture):
    payload = status_payload()
    del payload["entry"][0]["changes"][0]["value"]["statuses"][0]["status"]

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == []
    assert any("missing string id/status" in record.message for record in caplog.records)


async def test_status_type_confused_field_is_acked_not_500(handler, stub_app, caplog: pytest.LogCaptureFixture):
    # A well-signed status whose `status` (and `id`) is an unhashable list must be
    # 200-acked with no record_delivery_status call — never a `_DELIVERY_RECEIPTS`
    # dict-key TypeError('unhashable type: list') surfacing as a 500 that blocks
    # every legitimate item batched alongside it.
    payload = status_payload()
    status = payload["entry"][0]["changes"][0]["value"]["statuses"][0]
    status["id"] = ["wamid.OUT"]
    status["status"] = ["failed"]

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == []
    assert any("missing string id/status" in record.message for record in caplog.records)
