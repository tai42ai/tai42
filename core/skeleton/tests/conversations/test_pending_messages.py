"""The pending-message seam: the thread read a body running inside a turn asks to learn a newer message is waiting."""

from __future__ import annotations

import time

import pytest

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.pending import pending_messages
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_THREAD = "bridge:line:t"


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


@pytest.fixture
def store(monkeypatch) -> ConversationRecordStore:
    fake = FakeRecordRedis()
    fake.seed_route("line")
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationRecordStore(ConversationsSettings())


def _accepted(message_id: str, created_at: float, text: str) -> ConversationRecord:
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id=_THREAD,
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        provider_message_id=f"PID-{message_id}",
        origin="client",
        inbound_text=text,
        delivery_status=DeliveryStatus.ACCEPTED,
        created_at=created_at,
        updated_at=created_at,
    )


async def test_pending_messages_projects_the_later_accepted_messages(store):
    now = time.time()
    await store.create_record(_accepted("lead", now, "first"), intake_token="w")
    await store.create_record(_accepted("f1", now + 1, "second"), intake_token="w")
    await store.create_record(_accepted("f2", now + 2, "third"), intake_token="w")

    pending = await pending_messages(_THREAD, after="lead")
    assert [(m.message_id, m.text, m.accepted_at) for m in pending] == [
        ("f1", "second", now + 1),
        ("f2", "third", now + 2),
    ]


async def test_pending_messages_of_an_unknown_after_is_empty(store):
    now = time.time()
    await store.create_record(_accepted("lead", now, "first"), intake_token="w")
    # An ``after`` that names no record — an unknown lead — reads as nothing pending.
    assert await pending_messages(_THREAD, after="ghost") == []


async def test_the_facet_forwards_pending_messages_to_the_app():
    from tai42_skeleton.app.conversations_facet import ConversationsFacet

    class _App:
        def __init__(self) -> None:
            self.seen: tuple[str, str] | None = None

        async def _conversation_pending_messages(self, thread_id: str, *, after: str):
            self.seen = (thread_id, after)
            return []

    app = _App()
    facet = ConversationsFacet(app)  # pyright: ignore[reportArgumentType]
    assert await facet.pending_messages(_THREAD, after="lead") == []
    assert app.seen == (_THREAD, "lead")
