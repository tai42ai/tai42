"""Op-level oracles for the human answer operation.

These pin ``answer_interaction``'s store-logic branches DIRECTLY through the
operation (typed raises, not the route's JSON responses) — independent of the
router adapter and its body extractor — plus the format-validation helper's
server-bug guard and the declared destructive/error-class metadata. Redis is the
shared in-memory fake wired at the operation module's ``client_ctx`` seam.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest, InteractionResponse

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
)
from tai42_skeleton.operations import interactions as ops
from tai42_skeleton.operations.decorator import operation_metadata_of


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # the interactions surface is OFF with no Redis. These tests exercise the ON
    # feature, so configure its store — the fake connection still stands in; only the
    # presence gate reads this env var.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    settings = InteractionsSettings()
    monkeypatch.setattr(ops, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(ops, "interactions_settings", lambda: settings)
    store = InteractionStore(settings.key_prefix)
    return SimpleNamespace(settings=settings, store=store, fake=fake_redis)


def _req(store, fmt, *, iid="p1", gid="pg", payload=None) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="?",
        answer_format=fmt,
        format_payload=payload,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
    )


async def test_answer_unknown_interaction_raises_not_found(wired):
    with pytest.raises(NotFoundError, match="Interaction not found"):
        await ops.answer_interaction("ghost", "hi")


async def test_answer_external_raises_bad_request(wired):
    await wired.store.add(wired.fake, _req(wired.store, AnswerFormat.EXTERNAL, payload={"url": "x"}), idle_ttl=86400)
    with pytest.raises(BadRequestError, match="callback URL"):
        await ops.answer_interaction("p1", "x")


async def test_answer_text_success_then_conflict(wired):
    await wired.store.add(wired.fake, _req(wired.store, AnswerFormat.TEXT), idle_ttl=86400)
    assert await ops.answer_interaction("p1", "hello") == {"interaction_id": "p1", "status": "answered"}
    with pytest.raises(ConflictError, match="already answered"):
        await ops.answer_interaction("p1", "again")


async def test_answer_invalid_value_raises_bad_request(wired):
    await wired.store.add(wired.fake, _req(wired.store, AnswerFormat.CONFIRM), idle_ttl=86400)
    with pytest.raises(BadRequestError, match="must be a boolean"):
        await ops.answer_interaction("p1", "not-a-bool")


async def test_answer_form_schema_mismatch_names_the_field(wired):
    # The 400 text names the failing field (its json_path), so a human on any
    # surface can tell WHICH field failed — not a bare "is not of type 'integer'".
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    await wired.store.add(wired.fake, _req(wired.store, AnswerFormat.FORM, payload={"schema": schema}), idle_ttl=86400)
    with pytest.raises(BadRequestError, match="at count: 'abc' is not of type 'integer'"):
        await ops.answer_interaction("p1", {"count": "abc"})


async def test_answer_form_array_per_send_options_accept_and_reject(wired):
    # An inbox form ask with a multi-select (array-of-strings) property carrying a
    # per-send option list: the choices apply to the array's ITEMS, so a valid subset
    # answer is accepted while a value outside the per-send list is a 400 naming the
    # field. An enum placed on the array itself would reject even the valid answer.
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    }
    data = {"options": {"tags": [{"value": "a"}, {"value": "b"}]}}
    payload = {"schema": schema, "data": data}
    await wired.store.add(
        wired.fake, _req(wired.store, AnswerFormat.FORM, iid="fa", gid="fg", payload=payload), idle_ttl=86400
    )
    assert await ops.answer_interaction("fa", {"tags": ["a", "b"]}) == {"interaction_id": "fa", "status": "answered"}

    await wired.store.add(
        wired.fake, _req(wired.store, AnswerFormat.FORM, iid="fr", gid="fg2", payload=payload), idle_ttl=86400
    )
    with pytest.raises(BadRequestError, match="tags"):
        await ops.answer_interaction("fr", {"tags": ["a", "z"]})


def test_metadata_declares_destructive_and_the_full_error_set():
    meta = operation_metadata_of(ops.answer_interaction)
    assert meta.destructive is True
    assert meta.meta_executor is False
    assert meta.reload_gated is False
    assert set(meta.error_classes) == {
        BadRequestError,
        ConflictError,
        ForbiddenError,
        NotFoundError,
        PayloadTooLargeError,
    }


# -- cancel_interaction -------------------------------------------------------


def _async_park(store, *, iid="ap", gid="apg", expiry_minutes=60) -> InteractionRequest:
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=expiry_minutes)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=expiry,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=expiry,
    )


async def test_cancel_pending_succeeds_then_gone(wired):
    await wired.store.add(wired.fake, _req(wired.store, AnswerFormat.TEXT), idle_ttl=86400)
    assert await ops.cancel_interaction("p1") == {"interaction_id": "p1", "status": "cancelled"}
    # The state is gone (withdrawn); it no longer appears pending.
    assert await wired.store.get_state(wired.fake, "p1") is None
    assert [req.interaction_id for req in await wired.store.pending(wired.fake)] == []
    # Idempotent at the store seam: a re-cancel finds it gone → a clean 404, never a
    # double-teardown.
    with pytest.raises(NotFoundError, match="Interaction not found"):
        await ops.cancel_interaction("p1")


async def test_cancel_answered_raises_conflict(wired):
    await wired.store.add(wired.fake, _req(wired.store, AnswerFormat.TEXT), idle_ttl=86400)
    assert await ops.answer_interaction("p1", "done") == {"interaction_id": "p1", "status": "answered"}
    with pytest.raises(ConflictError, match="already answered"):
        await ops.cancel_interaction("p1")


async def test_cancel_unknown_raises_not_found(wired):
    with pytest.raises(NotFoundError, match="Interaction not found"):
        await ops.cancel_interaction("ghost")


async def test_cancel_is_answer_format_agnostic_external(wired):
    # Unlike the answer door (which rejects EXTERNAL), cancel WITHDRAWS a pending ask of
    # any format — an external ask is a pending ask an operator may withdraw.
    await wired.store.add(wired.fake, _req(wired.store, AnswerFormat.EXTERNAL, payload={"url": "x"}), idle_ttl=86400)
    assert await ops.cancel_interaction("p1") == {"interaction_id": "p1", "status": "cancelled"}


def _caller_park(store, *, iid="c1", gid="cg", expiry_minutes=60) -> InteractionRequest:
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=expiry_minutes)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=expiry,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=expiry,
        to="caller",
    )


async def test_answer_caller_ask_raises_conflict(wired):
    # A caller ask is addressed to the calling run and resolved only by that run resuming;
    # the human answer door refuses it loudly, never answers it.
    await wired.store.add(wired.fake, _caller_park(wired.store), idle_ttl=86400, to="caller")
    with pytest.raises(ConflictError, match="calling run"):
        await ops.answer_interaction("c1", "hi")


async def test_cancel_caller_ask_accepted(wired):
    # Cancel is a TEARDOWN door, so it ACCEPTS a caller ask (unlike the answer door which
    # refuses it), routing the withdrawal through the kill seam.
    await wired.store.add(wired.fake, _caller_park(wired.store), idle_ttl=86400, to="caller")
    assert await ops.cancel_interaction("c1") == {"interaction_id": "c1", "status": "cancelled"}
    assert await wired.store.get_state(wired.fake, "c1") is None


async def test_list_interactions_hides_caller_ask(wired):
    # The inbox read hides a caller ask (the store's ``to`` filter) while a co-pending user
    # ask still lists.
    await wired.store.add(wired.fake, _caller_park(wired.store, iid="c1", gid="cg"), idle_ttl=86400, to="caller")
    await wired.store.add(wired.fake, _req(wired.store, AnswerFormat.TEXT, iid="u1", gid="ug"), idle_ttl=86400)
    result = await ops.list_interactions()
    assert [item["interaction_id"] for item in result["items"]] == ["u1"]
    assert result["total"] == 1


async def test_list_pending_interactions_hides_caller_ask(wired):
    # The parked-ask audit hides a caller ask (async, so expiry-indexed) while a user park
    # still lists.
    await wired.store.add(wired.fake, _caller_park(wired.store, iid="c1", gid="cg"), idle_ttl=86400, to="caller")
    await wired.store.add(wired.fake, _async_park(wired.store, iid="u1", gid="ug"), idle_ttl=86400)
    result = await ops.list_pending_interactions()
    assert [item["interaction_id"] for item in result["items"]] == ["u1"]
    assert result["count"] == 1


async def test_cancel_async_park_fires_no_continuation(wired):
    # Cancel tears the park down via ``prune_pending``, which NEVER enqueues a
    # continuation: the parked flow can never resume. Proof: a late answer claims
    # nothing and the expiry reaper has nothing to fire.
    await wired.store.add(wired.fake, _async_park(wired.store), idle_ttl=86400, continuation_fingerprint="fp")
    assert await ops.cancel_interaction("ap") == {"interaction_id": "ap", "status": "cancelled"}
    claimed = await wired.store.record_answer(
        wired.fake,
        InteractionResponse(interaction_id="ap", answer="late", answered_by="op", answered_at=datetime.now(UTC)),
        group_id="apg",
        reply_ttl=60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=0,
    )
    assert claimed is False
    assert await wired.store.due_expiries(wired.fake, datetime.now(UTC) + timedelta(days=1)) == []


def test_cancel_metadata_declares_destructive_and_error_set():
    meta = operation_metadata_of(ops.cancel_interaction)
    assert meta.destructive is True
    assert meta.meta_executor is False
    assert meta.reload_gated is False
    # A subset of the answer door's set: no body is parsed, so no BadRequest/413.
    assert set(meta.error_classes) == {ConflictError, ForbiddenError, NotFoundError}


async def test_cancel_off_when_store_unconfigured_raises_not_found(monkeypatch):
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    with pytest.raises(NotFoundError, match="Interaction not found"):
        await ops.cancel_interaction("ghost")


# -- the store-unconfigured OFF gate ------------------------------------


async def test_answer_off_when_store_unconfigured_raises_not_found(monkeypatch):
    # With no interactions Redis no interaction can exist — the answer door raises the
    # SAME 404 as a genuine miss (delenv BOTH vars, overriding the autouse setenv), so
    # the door is no oracle for the store's absence.
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    with pytest.raises(NotFoundError, match="Interaction not found"):
        await ops.answer_interaction("ghost", "hi")
