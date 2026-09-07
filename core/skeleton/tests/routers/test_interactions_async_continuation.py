"""The callback answer door fires an async park's stored continuation ONCE, under
the STORED identity, through the same shared post-claim seam the authenticated
answer door uses — so a park answered over its public callback resumes work
exactly like one answered in the inbox.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.routers import interactions as router


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    settings = InteractionsSettings(public_base_url="https://cb.example")
    monkeypatch.setattr(router, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(router, "interactions_settings", lambda: settings)
    # The detached fire's own client (the clear of the durable due-record) opens
    # through the continuation module's seam — point it at the same fake.
    monkeypatch.setattr(continuation_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(continuation_module, "interactions_settings", lambda: settings)
    store = InteractionStore(settings.key_prefix)
    return SimpleNamespace(settings=settings, store=store, fake=fake_redis)


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    async def _stub(identity, fingerprint, tool, interaction_id, answer, park_context=None):
        calls.append({"identity": identity, "fingerprint": fingerprint, "answer": answer})

    monkeypatch.setattr(continuation_module, "_run_continuation", _stub)
    return calls


def _async_external_req(store: InteractionStore, iid: str) -> InteractionRequest:
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=1)
    return InteractionRequest(
        interaction_id=iid,
        group_id="ag",
        question="?",
        answer_format=AnswerFormat.EXTERNAL,
        format_payload={"url": "https://cb.example/x"},
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=deadline,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=deadline,
    )


async def test_callback_door_fires_continuation_under_stored_identity(wired, captured):
    await wired.store.add(
        wired.fake, _async_external_req(wired.store, "c1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    state = await wired.store.get_state(wired.fake, "c1")
    assert state is not None
    response = await router._record_callback_answer(
        wired.fake, wired.store, wired.settings, "tkt", "c1", state, {"ok": True}
    )
    assert response.status_code == 200
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(captured) == 1
    assert captured[0]["identity"] == "svc-key"
    assert captured[0]["fingerprint"] == "fp-1"
    assert captured[0]["answer"] == {"ok": True}
