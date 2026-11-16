"""One message resuming two questions of a turn: the turn's indicator pauses for the open one and shows again."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionRequest,
    ResumeBuffered,
    ResumeItem,
    reset_park_completion,
    set_park_completion,
)

from tai42_skeleton.conversations import working_signal
from tai42_skeleton.conversations.turn.completion_delivery import COMPLETION_TOOL_NAME
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore

from ._working_signal_support import (
    TARGET,
    CountingChannel,
    ManualClock,
    SendStore,
    actives,
    clears,
    install_app,
    install_clock,
    install_run_target,
    install_store,
    record,
    settle,
    stop_all,
)

RUN_DELIVERY = "rd-visit"
THREAD = "bridge:line:+15550002222"


@pytest.fixture
async def env(monkeypatch, fake_redis, fake_client_ctx):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS", "60")
    monkeypatch.setenv("CONVERSATIONS_WORKING_SIGNAL_REFRESH_MARGIN_SECONDS", "5")
    install_clock(monkeypatch, ManualClock())
    channel = CountingChannel()
    install_app(monkeypatch, channel)
    sends = SendStore()
    install_store(monkeypatch, sends)
    install_run_target(monkeypatch)
    settings = InteractionsSettings()
    for module in (visit_module, continuation_module):
        monkeypatch.setattr(module, "client_ctx", fake_client_ctx)
        monkeypatch.setattr(module, "interactions_settings", lambda: settings)
    store = InteractionStore(settings.key_prefix)
    yield channel, sends, store, fake_redis
    await stop_all()


async def _park(store: InteractionStore, fake_redis, iid: str) -> None:
    now = datetime.now(UTC)
    request = InteractionRequest(
        interaction_id=iid,
        group_id="g1",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(hours=1),
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=now + timedelta(hours=1),
        run_delivery_id=RUN_DELIVERY,
    )
    await store.add(fake_redis, request, idle_ttl=86400, run_delivery_id=RUN_DELIVERY)


async def test_two_resumes_of_one_turn_pause_for_the_open_sibling_and_show_again_for_the_last(env, monkeypatch):
    channel, sends, store, fake_redis = env
    for iid in ("q1", "q2"):
        await _park(store, fake_redis, iid)
        sends.ask_on(iid)
    seen: dict[str, tuple[int, int]] = {}

    async def _drive(identity, fingerprint, tool, interaction_id, answer, *args, **kwargs):
        await settle()
        seen[interaction_id] = (len(actives(channel)), len(clears(channel)))
        return ResumeBuffered(remaining_ids=["q2"]) if interaction_id == "q1" else {"reply": "done"}

    monkeypatch.setattr(continuation_module, "_run_continuation", _drive)
    turn = record("turn-1")
    working_signal.start_turn(turn)
    await settle()
    assert actives(channel) == [(True, TARGET[2], "wamid-1")]

    token = set_park_completion(COMPLETION_TOOL_NAME, {"message_id": turn.message_id}, thread_id=THREAD)
    try:
        await visit_module._drive_resumes(
            store,
            None,
            None,
            [ResumeItem(id="q1", payload="a"), ResumeItem(id="q2", payload="b")],
            {},
            True,
            {"q1": "a", "q2": "b"},
            None,
        )
    finally:
        reset_park_completion(token)

    assert seen == {"q1": (1, 1), "q2": (2, 1)}
    assert actives(channel)[-1] == (True, TARGET[2], "wamid-1")
    working_signal.stop(turn.message_id)
    await settle()
    assert len(clears(channel)) == 2
