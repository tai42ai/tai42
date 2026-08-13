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
from tai42_contract.interactions import AnswerFormat, InteractionRequest

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


def test_schema_error_message_field_paths():
    # ``_schema_mismatch`` returns ``(message, field)``: a field-level error names the
    # field (``$``-root and leading ``.`` stripped) in BOTH the message and the bare
    # ``field``; a root-level error and a pathless validator carry ``field=None`` and
    # fall back to the bare message.
    root = ops._schema_mismatch(
        {"y": 1}, {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    )
    assert root == ("answer does not match schema: 'x' is a required property", None)
    field = ops._schema_mismatch({"count": "abc"}, {"type": "object", "properties": {"count": {"type": "integer"}}})
    assert field == ("answer does not match schema at count: 'abc' is not of type 'integer'", "count")
    nested = ops._schema_mismatch(
        {"a": {"b": "x"}},
        {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "integer"}}}}},
    )
    assert nested == ("answer does not match schema at a.b: 'x' is not of type 'integer'", "a.b")
    # A malformed stored schema raises SchemaError, whose json_path points into the
    # schema (``properties.x.type``) — a location no answering human owns; the message
    # carries NO "at ..." path, only the bare reason, and the field is ``None``.
    schema_error = ops._schema_mismatch({"x": 1}, {"type": "object", "properties": {"x": {"type": "bogus"}}})
    assert schema_error is not None
    message, field_path = schema_error
    assert field_path is None
    assert "at " not in message
    assert message.startswith("answer does not match schema: ")


def test_validate_answer_external_is_a_server_bug(wired):
    # EXTERNAL is rejected by the answer door before ``_validate_answer`` runs, so a
    # direct call is the defensive server-bug path — a loud 500, never a 4xx.
    req = _req(wired.store, AnswerFormat.EXTERNAL, payload={"url": "x"})
    with pytest.raises(RuntimeError, match="unhandled answer_format"):
        ops._validate_answer(req, "x")


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


# -- the store-unconfigured OFF gate ------------------------------------


async def test_answer_off_when_store_unconfigured_raises_not_found(monkeypatch):
    # With no interactions Redis no interaction can exist — the answer door raises the
    # SAME 404 as a genuine miss (delenv BOTH vars, overriding the autouse setenv), so
    # the door is no oracle for the store's absence.
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    with pytest.raises(NotFoundError, match="Interaction not found"):
        await ops.answer_interaction("ghost", "hi")
