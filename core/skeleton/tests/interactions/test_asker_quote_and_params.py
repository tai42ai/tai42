"""What an asker receives beside the answer: the message the answering reply quoted and the
answer's ``params``.

A sync ask returns an ``AskAnswer``; an async ask's continuation tool runs with
``{interaction_id, ask_result}``, ``ask_result`` being the same ``AskAnswer`` as JSON. The
quoted message's id and the params travel with the answer through the interaction store (the
reply list and the continuation-due record); the quote's text is resolved when the asker is
handed the answer.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tai42_contract.conversations import ReplyTo, ReplyToStatus
from tai42_contract.interactions import (
    AnswerFormat,
    AskAnswer,
    InteractionRequest,
    InteractionResponse,
    SuspendedInteraction,
)
from tai42_contract.secrets import SecretValue

from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.interactions.ask import wait as wait_module
from tai42_skeleton.interactions.ask.timing import DeadlineWindow
from tai42_skeleton.interactions.continuation_dispatch import EXPIRY_ANSWER, fire_continuation_after_claim
from tai42_skeleton.interactions.store import ContinuationDue
from tai42_skeleton.operations import interactions as ops

from ._continuation_support import async_req, configure_interactions_store, drain, make_captured, make_wired

_QUOTE = ReplyTo(message_id="M-1", status=ReplyToStatus.FOUND, text="the earlier message")
_PARAMS = {"button_payload": "P1"}


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


@pytest.fixture
def resolved(monkeypatch) -> list[tuple[str, str]]:
    """Every ``resolve_ask_reply_to`` call the asker seams make; each resolves to ``_QUOTE``."""
    calls: list[tuple[str, str]] = []

    async def _resolve(interaction_id: str, medium_message_id: str) -> ReplyTo:
        calls.append((interaction_id, medium_message_id))
        return _QUOTE

    monkeypatch.setattr(wait_module, "resolve_ask_reply_to", _resolve)
    monkeypatch.setattr(continuation_module, "resolve_ask_reply_to", _resolve)
    return calls


def _response(iid: str, answer="yes", *, reply_to: str | None = None, params=None) -> InteractionResponse:
    return InteractionResponse(
        interaction_id=iid,
        answer=answer,
        answered_by="tester",
        answered_at=datetime.now(UTC),
        medium_message_id="M-2",
        reply_to_medium_message_id=reply_to,
        params=params,
    )


def _sync_req(store, iid: str, *, sensitive: bool = False) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=iid,
        group_id="sg",
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
        sensitive=sensitive,
    )


def _window() -> DeadlineWindow:
    now = datetime.now(UTC)
    return DeadlineWindow(
        budget=5,
        created_at=now,
        timeout_at=now + timedelta(seconds=5),
        deadline=asyncio.get_running_loop().time() + 5,
    )


async def _answered_sync(wired, iid: str, response: InteractionResponse, *, sensitive: bool = False):
    await wired.store.add(wired.fake, _sync_req(wired.store, iid, sensitive=sensitive), idle_ttl=100)
    assert await wired.store.record_answer(wired.fake, response, "sg", reply_ttl=60) is True
    return await wait_module.await_answer(
        wired.store,
        _window(),
        interaction_id=iid,
        group="sg",
        reply_key=wired.store.reply_key(iid),
        question="proceed?",
        sensitive=sensitive,
    )


# --- a waiting (sync) ask -------------------------------------------------------------------


async def test_a_sync_ask_returns_the_answer_with_its_quote_and_params(wired, resolved):
    result = await _answered_sync(wired, "s1", _response("s1", reply_to="M-1", params=_PARAMS))
    assert result == AskAnswer(answer="yes", reply_to=_QUOTE, params=_PARAMS)
    assert resolved == [("s1", "M-1")]


async def test_a_sync_ask_answered_quoting_nothing_resolves_nothing(wired, resolved):
    result = await _answered_sync(wired, "s2", _response("s2"))
    assert result == AskAnswer(answer="yes", reply_to=None, params=None)
    assert resolved == []


async def test_a_sensitive_sync_ask_wraps_the_answer_and_still_carries_its_quote_and_params(wired, resolved):
    result = await _answered_sync(
        wired, "s3", _response("s3", answer="hunter2", reply_to="M-1", params=_PARAMS), sensitive=True
    )
    assert isinstance(result, AskAnswer)
    assert isinstance(result.answer, SecretValue)
    assert result.answer.reveal() == "hunter2"
    assert "hunter2" not in repr(result)
    assert result.reply_to == _QUOTE
    assert result.params == _PARAMS


# --- the continuation-due record -------------------------------------------------------------


async def test_the_due_record_carries_the_quote_id_and_params_and_reads_them_back(wired):
    await wired.store.add(wired.fake, async_req(wired.store, iid="a1"), idle_ttl=86400, continuation_fingerprint="fp")
    assert await wired.store.record_answer(
        wired.fake,
        _response("a1", reply_to="M-1", params=_PARAMS),
        "ag",
        reply_ttl=60,
        continuation_due_ttl=86400,
        continuation_first_attempt_at_ms=0,
    )
    raw = await wired.fake.hgetall(wired.store.continuation_due_key("a1"))
    assert raw["reply_to_medium_message_id"] == "M-1"
    assert json.loads(raw["params"]) == _PARAMS
    assert "the earlier message" not in json.dumps(raw)

    due = await wired.store.claim_continuation_retry(wired.fake, "a1", datetime.now(UTC), 1000, 10000)
    assert isinstance(due, ContinuationDue)
    assert due.reply_to_medium_message_id == "M-1"
    assert due.params == _PARAMS


async def test_a_due_record_of_an_answer_with_no_quote_and_no_params_carries_neither(wired):
    await wired.store.add(wired.fake, async_req(wired.store, iid="a2"), idle_ttl=86400, continuation_fingerprint="fp")
    assert await wired.store.record_answer(
        wired.fake, _response("a2"), "ag", reply_ttl=60, continuation_due_ttl=86400, continuation_first_attempt_at_ms=0
    )
    raw = await wired.fake.hgetall(wired.store.continuation_due_key("a2"))
    assert "reply_to_medium_message_id" not in raw
    assert "params" not in raw
    due = await wired.store.claim_continuation_retry(wired.fake, "a2", datetime.now(UTC), 1000, 10000)
    assert isinstance(due, ContinuationDue)
    assert due.reply_to_medium_message_id is None
    assert due.params is None


# --- every resume door hands the id and params to the drive ------------------------------------


async def test_a_claimed_channel_answer_fires_the_continuation_with_its_quote_id_and_params(wired, monkeypatch):
    captured = make_captured(monkeypatch)
    request = async_req(wired.store, iid="d1")
    await wired.store.add(wired.fake, request, idle_ttl=86400, continuation_fingerprint="fp")
    response = _response("d1", reply_to="M-1", params=_PARAMS)
    assert await wired.store.record_answer(
        wired.fake, response, "ag", reply_ttl=60, continuation_due_ttl=86400, continuation_first_attempt_at_ms=0
    )
    await fire_continuation_after_claim(wired.fake, wired.store, request, response)
    await drain()
    assert [(c["answer"], c["reply_to_medium_message_id"], c["params"]) for c in captured] == [("yes", "M-1", _PARAMS)]


async def test_an_operator_answer_fires_the_continuation_with_no_quote_and_no_params(wired, monkeypatch):
    captured = make_captured(monkeypatch)
    await wired.store.add(wired.fake, async_req(wired.store, iid="d2"), idle_ttl=86400, continuation_fingerprint="fp")
    assert await ops.answer_interaction("d2", "go") == {"interaction_id": "d2", "status": "answered"}
    await drain()
    assert [(c["answer"], c["reply_to_medium_message_id"], c["params"]) for c in captured] == [("go", None, None)]


async def test_an_expired_park_fires_the_continuation_with_no_quote_and_no_params(wired, monkeypatch):
    captured = make_captured(monkeypatch)
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(
        wired.fake, async_req(wired.store, iid="d3", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp"
    )
    assert await reaper_module.reap_expired_parks_once() == 1
    await drain()
    assert [(c["answer"], c["reply_to_medium_message_id"], c["params"]) for c in captured] == [
        (EXPIRY_ANSWER, None, None)
    ]


async def test_a_redelivery_fires_the_continuation_with_the_due_records_quote_id_and_params(wired, monkeypatch):
    captured = make_captured(monkeypatch)
    await wired.store.add(wired.fake, async_req(wired.store, iid="d4"), idle_ttl=86400, continuation_fingerprint="fp")
    assert await wired.store.record_answer(
        wired.fake,
        _response("d4", reply_to="M-1", params=_PARAMS),
        "ag",
        reply_ttl=60,
        continuation_due_ttl=86400,
        continuation_first_attempt_at_ms=0,
    )
    assert await reaper_module.redeliver_due_continuations_once() == 1
    await drain()
    assert [(c["answer"], c["reply_to_medium_message_id"], c["params"]) for c in captured] == [("yes", "M-1", _PARAMS)]


# --- the continuation tool's arguments -------------------------------------------------------


@pytest.fixture
def ran(monkeypatch) -> list[dict]:
    """The arguments of every ``run_tool`` a real ``_run_continuation`` makes, the identity bind faked."""
    seen: list[dict] = []

    async def _run_tool(tool, arguments, *, continues_chain=None):
        seen.append(arguments)
        return SuspendedInteraction(interaction_id="next")

    @asynccontextmanager
    async def _bind(identity, *, bound_fingerprint=""):
        yield

    monkeypatch.setattr(continuation_module, "tai42_app", SimpleNamespace(tools=SimpleNamespace(run_tool=_run_tool)))
    monkeypatch.setattr(continuation_module, "bind_execution_identity", _bind)
    return seen


async def test_the_continuation_tool_runs_with_the_resolved_quote_and_the_params(wired, ran, resolved):
    await continuation_module._run_continuation(
        "svc-key", None, "resume_tool", "c1", "yes", reply_to_medium_message_id="M-1", params=_PARAMS
    )
    assert ran == [
        {
            "interaction_id": "c1",
            "ask_result": {"answer": "yes", "reply_to": _QUOTE.model_dump(mode="json"), "params": _PARAMS},
        }
    ]
    assert resolved == [("c1", "M-1")]


async def test_a_continuation_with_no_quote_runs_with_null_reply_to_and_resolves_nothing(wired, ran, resolved):
    await continuation_module._run_continuation(
        "svc-key", None, "resume_tool", "c2", "yes", reply_to_medium_message_id=None, params=None
    )
    assert ran == [{"interaction_id": "c2", "ask_result": {"answer": "yes", "reply_to": None, "params": None}}]
    assert resolved == []
