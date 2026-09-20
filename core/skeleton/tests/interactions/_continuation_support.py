"""Shared wiring for the async-park continuation suites: the store/settings/fake
harness, the detached-fire capture, the async-request builder, and the durable
due-record seeder the outbox/redelivery/dispatch tests build on."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from tai42_contract.interactions import AnswerFormat, InteractionRequest

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.operations import interactions as ops

from .._fakes.interactions_redis import FakeRedis


def configure_interactions_store(monkeypatch) -> None:
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


def make_wired(monkeypatch, fake_redis, fake_client_ctx) -> SimpleNamespace:
    settings = InteractionsSettings()
    monkeypatch.setattr(ops, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(ops, "interactions_settings", lambda: settings)
    monkeypatch.setattr(reaper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(reaper_module, "interactions_settings", lambda: settings)
    # The detached fire's own client (the clear of the durable due-record) opens
    # through the continuation module's seam — point it at the same fake.
    monkeypatch.setattr(continuation_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(continuation_module, "interactions_settings", lambda: settings)
    store = InteractionStore(settings.key_prefix)
    return SimpleNamespace(settings=settings, store=store, fake=fake_redis)


def make_captured(monkeypatch) -> list[dict]:
    # Capture the detached continuation's rebind + dispatch args WITHOUT running the
    # real execution-identity bind / run_tool machinery.
    calls: list[dict] = []

    async def _stub(identity, fingerprint, tool, interaction_id, answer, park_context=None):
        calls.append(
            {
                "identity": identity,
                "fingerprint": fingerprint,
                "tool": tool,
                "interaction_id": interaction_id,
                "answer": answer,
                "park_context": park_context,
            }
        )

    monkeypatch.setattr(continuation_module, "_run_continuation", _stub)
    return calls


def async_req(store: InteractionStore, *, iid: str, gid: str = "ag", expiry_at: datetime | None = None):
    now = datetime.now(UTC)
    deadline = expiry_at or now + timedelta(hours=1)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=deadline,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=deadline,
    )


async def drain() -> None:
    # Let the detached continuation task run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def seed_due(store, fake, iid, *, answer, first_attempt_at_ms) -> None:
    # Seed a durable continuation-due record + its index member directly (the shape the
    # atomic claim writes), for redelivery tests that drive the reaper machinery without
    # a full answer round trip.
    await fake.hset(
        store.continuation_due_key(iid),
        mapping={
            "tool": "resume_tool",
            "identity": "svc-key",
            "fingerprint": "fp-1",
            "answer": json.dumps(answer),
            "attempts": "0",
        },
    )
    await fake.zadd(store.continuation_due_index_key, {iid: first_attempt_at_ms})


class RecordingHooks:
    """Captures every ``on_event`` the reaper emits."""

    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    async def on_event(self, topic, payload, *, tool_kwargs_override=None) -> None:
        self.events.append(SimpleNamespace(topic=topic, payload=payload))


class EvalBarrierRedis(FakeRedis):
    """A fake whose ``eval`` — the phantom-purge call every ``add`` makes AFTER its
    pre-write reads and BEFORE it commits — is a two-party rendezvous: the first
    ``add`` to reach it suspends until the second arrives, so both parks are past
    every read before either writes. That is the exact interleave the concurrent
    same-group TTL race needs: the last committer must not have seen the other
    park's write."""

    def __init__(self) -> None:
        super().__init__()
        self.arrived = 0
        self._both_in = asyncio.Event()

    async def eval(self, script, numkeys, *keys_and_args):
        self.arrived += 1
        if self.arrived >= 2:
            self._both_in.set()
        else:
            await self._both_in.wait()
        return await super().eval(script, numkeys, *keys_and_args)
