"""Tier 2 of the send-outcome monitoring layer: the flow-send receipt index.

Covers the ``provider_message_id -> {trace_id, span_id}`` index write, the webhook hit
path posting a ``delivery_receipt`` event back onto the flow trace (delivered vs failed
level, nested under the send span), the unknown-id miss staying benign, the
store-unconfigured no-op, and TTL expiry dropping the entry.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from tai42_contract.conversations import DeliveryReceipt
from tai42_contract.monitoring import MonitoringLevel

from tai42_skeleton.channels import send_receipts
from tai42_skeleton.monitoring import init_monitoring, reset_monitoring

from .._fakes.recording_monitoring import RecordingMonitoring


class _FakeKV:
    """A minimal string KV with lazily-enforced absolute TTL. Tests advance ``now`` to
    drive expiry, mirroring the interactions ``FakeRedis`` clock."""

    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _expired(self, key: str) -> bool:
        exp = self._expiry.get(key)
        if exp is not None and self.now >= exp:
            self._strings.pop(key, None)
            self._expiry.pop(key, None)
            return True
        return False

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._strings[key] = value
        if ex is not None:
            self._expiry[key] = self.now + ex

    async def get(self, key: str) -> str | None:
        self._expired(key)
        return self._strings.get(key)


@pytest.fixture
def kv(monkeypatch: pytest.MonkeyPatch) -> _FakeKV:
    fake = _FakeKV()

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, **kwargs):
        yield fake

    settings = SimpleNamespace(redis=None, key_prefix="interactions:", send_receipt_index_ttl_seconds=100)
    monkeypatch.setattr(send_receipts, "client_ctx", _ctx)
    monkeypatch.setattr(send_receipts, "interactions_settings", lambda: settings)
    monkeypatch.setattr(send_receipts, "interactions_store_configured", lambda: True)
    return fake


@pytest.fixture
def backend() -> Iterator[RecordingMonitoring]:
    reset_monitoring()
    backend = RecordingMonitoring()
    init_monitoring(backend)
    yield backend
    reset_monitoring()


async def test_index_write_then_delivered_receipt_posts_event(kv: _FakeKV, backend: RecordingMonitoring) -> None:
    await send_receipts.index_flow_send("whatsapp", ["wamid-1"], trace_id="trace-1", span_id="span-1")

    hit = await send_receipts.record_flow_send_receipt("whatsapp", "wamid-1", DeliveryReceipt.DELIVERED)

    assert hit is True
    (event,) = backend.writer.events
    assert event["name"] == "delivery_receipt"
    assert event["level"] is MonitoringLevel.DEFAULT
    # Explicit trace context (the webhook is detached) nested under the send span.
    assert event["trace_context"].trace_id == "trace-1"
    assert event["trace_context"].parent_span_id == "span-1"
    assert event["input"] == {"provider_message_id": "wamid-1", "status": "delivered", "errors": None}


async def test_failed_receipt_posts_error_level_event(kv: _FakeKV, backend: RecordingMonitoring) -> None:
    await send_receipts.index_flow_send("whatsapp", ["wamid-2"], trace_id="trace-9", span_id="span-9")

    hit = await send_receipts.record_flow_send_receipt(
        "whatsapp", "wamid-2", DeliveryReceipt.FAILED, errors=[{"code": 131026}]
    )

    assert hit is True
    (event,) = backend.writer.events
    assert event["level"] is MonitoringLevel.ERROR
    assert event["input"] == {
        "provider_message_id": "wamid-2",
        "status": "failed",
        "errors": [{"code": 131026}],
    }


async def test_index_read_error_resolves_to_a_benign_miss(
    kv: _FakeKV, backend: RecordingMonitoring, caplog: pytest.LogCaptureFixture
) -> None:
    # A monitoring-store (interactions Redis) outage on the index READ must never break
    # receipt ingestion: this seam runs in the webhook's ``except LookupError`` fallback, so
    # a raised error would 500 an otherwise-healthy delivery-status webhook. The read is
    # caught-and-logged and resolves to a benign miss (False), emitting no event.
    async def _boom(key: str) -> str | None:
        raise ConnectionError("interactions redis down")

    kv.get = _boom  # type: ignore[method-assign]

    with caplog.at_level("WARNING"):
        hit = await send_receipts.record_flow_send_receipt("whatsapp", "wamid-x", DeliveryReceipt.DELIVERED)

    assert hit is False
    assert backend.writer.events == []
    assert any("index read failed" in record.message for record in caplog.records)


async def test_unknown_id_is_benign_no_event(kv: _FakeKV, backend: RecordingMonitoring) -> None:
    hit = await send_receipts.record_flow_send_receipt("whatsapp", "never-sent", DeliveryReceipt.DELIVERED)

    assert hit is False
    assert backend.writer.events == []


async def test_unconfigured_store_is_a_no_op(monkeypatch: pytest.MonkeyPatch, backend: RecordingMonitoring) -> None:
    monkeypatch.setattr(send_receipts, "interactions_store_configured", lambda: False)

    # Neither the write nor the lookup touches Redis when the store is unconfigured.
    await send_receipts.index_flow_send("whatsapp", ["wamid-3"], trace_id="t", span_id="s")
    hit = await send_receipts.record_flow_send_receipt("whatsapp", "wamid-3", DeliveryReceipt.DELIVERED)

    assert hit is False
    assert backend.writer.events == []


async def test_empty_id_list_writes_nothing(kv: _FakeKV, backend: RecordingMonitoring) -> None:
    # A channel that exposes no correlatable id indexes nothing.
    await send_receipts.index_flow_send("whatsapp", [], trace_id="t", span_id="s")

    assert await send_receipts.record_flow_send_receipt("whatsapp", "anything", DeliveryReceipt.DELIVERED) is False


async def test_ttl_expiry_drops_the_index_entry(kv: _FakeKV, backend: RecordingMonitoring) -> None:
    await send_receipts.index_flow_send("whatsapp", ["wamid-4"], trace_id="t", span_id="s")

    # Past the index TTL (100s), the entry is gone and a late receipt no longer correlates.
    kv.advance(101)

    hit = await send_receipts.record_flow_send_receipt("whatsapp", "wamid-4", DeliveryReceipt.DELIVERED)
    assert hit is False
    assert backend.writer.events == []


async def test_channel_qualifies_the_index_key(kv: _FakeKV, backend: RecordingMonitoring) -> None:
    # The same provider id under a different channel is a distinct entry — never cross-resolved.
    await send_receipts.index_flow_send("whatsapp", ["shared-id"], trace_id="t-wa", span_id="s-wa")

    assert await send_receipts.record_flow_send_receipt("twilio", "shared-id", DeliveryReceipt.DELIVERED) is False
    assert await send_receipts.record_flow_send_receipt("whatsapp", "shared-id", DeliveryReceipt.DELIVERED) is True
