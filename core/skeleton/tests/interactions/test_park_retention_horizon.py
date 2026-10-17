"""An async park's keys live one retention horizon past its deadline.

Every key an async park writes (state hash, group stream, pending count, run index,
callback ticket) lives ``remaining-to-expiry + idle_ttl_seconds``, so the expiry reaper
resolves the park however late it runs within that horizon. A sync question's keys, and a
park already past its deadline when written, keep the flat ``idle_ttl``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tai42_contract.interactions import AnswerFormat, InteractionRequest

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions.ask.timing import DeadlineWindow, mint_callback_ticket
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store.ttl import _key_ttl

_IDLE = 100


def _request(*, mode: str, expiry_in: float, iid: str = "h1") -> InteractionRequest:
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=expiry_in)
    return InteractionRequest(
        interaction_id=iid,
        group_id="hg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=f"reply:{iid}",
        created_at=now,
        timeout_at=deadline,
        mode=mode,  # type: ignore[arg-type]
        continuation_tool="resume_tool" if mode == "async" else None,
        continuation_identity="svc-key" if mode == "async" else None,
        expiry_at=deadline if mode == "async" else None,
    )


def test_an_async_park_keeps_its_keys_one_horizon_past_its_deadline():
    request = _request(mode="async", expiry_in=300)
    assert request.expiry_at is not None
    now_ms = int(request.expiry_at.timestamp() * 1000) - 300_000

    assert _key_ttl(request, _IDLE, now_ms) == 300 + _IDLE


def test_a_sync_question_keeps_the_flat_idle_ttl():
    request = _request(mode="sync", expiry_in=300)

    assert _key_ttl(request, _IDLE, int(datetime.now(UTC).timestamp() * 1000)) == _IDLE


def test_a_park_written_past_its_deadline_keeps_one_horizon():
    request = _request(mode="async", expiry_in=-50)

    assert _key_ttl(request, _IDLE, int(datetime.now(UTC).timestamp() * 1000)) == _IDLE


async def test_add_gives_every_park_key_the_horizon(fake_redis):
    store = InteractionStore("hz:")
    request = _request(mode="async", expiry_in=300)
    await store.add(fake_redis, request, idle_ttl=_IDLE, ticket="tk", ticket_ttl=300 + _IDLE, run_delivery_id="rd-hz")

    for key in (
        store.state_key("h1"),
        store.group_key("hg"),
        store.count_key("hg"),
        store.run_parks_key("rd-hz"),
        store.ticket_key("tk"),
    ):
        assert 300 + _IDLE - 1 <= await fake_redis.ttl(key) <= 300 + _IDLE, key


def test_an_async_park_ticket_lives_one_horizon_past_its_deadline():
    settings = InteractionsSettings(public_base_url="https://host.example", idle_ttl_seconds=_IDLE)
    now = datetime.now(UTC)
    window = DeadlineWindow(budget=60.0, created_at=now, timeout_at=now + timedelta(seconds=300), deadline=0.0)

    ticket = mint_callback_ticket(settings, window, "async", force=True)

    assert ticket is not None
    assert ticket.ticket_ttl == 300 + _IDLE


def test_a_sync_question_ticket_lives_to_its_deadline():
    settings = InteractionsSettings(public_base_url="https://host.example", idle_ttl_seconds=_IDLE)
    now = datetime.now(UTC)
    window = DeadlineWindow(budget=60.0, created_at=now, timeout_at=now + timedelta(seconds=60), deadline=0.0)

    ticket = mint_callback_ticket(settings, window, "sync", force=True)

    assert ticket is not None
    assert ticket.ticket_ttl == 60
