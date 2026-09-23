"""The call chain and run-delivery wiring on the skeleton side.

``run_tool``/``dispatch_scope`` forward the ``continues_chain`` seam keyword into the
call frame (SET branch); an async park records its ``asked_by`` as the live chain
WITHOUT the ask-performing frame; and the continuation runners pass the origin chain
on the one re-entry dispatch — from ``request.asked_by`` (detached) and ``due.asked_by``
(reaper redelivery) alike.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from tai42_contract.interactions import AnswerFormat, AnswerMismatchPolicy, InteractionRequest
from tai42_contract.tools import current_call_chain, tool_call_frame

if TYPE_CHECKING:
    from tai42_skeleton.app.server import TaiMCP

from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions.ask.park import AsyncParkBinding
from tai42_skeleton.interactions.ask.persist import build_request
from tai42_skeleton.interactions.ask.timing import DeadlineWindow
from tai42_skeleton.interactions.store import ContinuationDue, records
from tai42_skeleton.tools.dispatch_scope import dispatch_scope

from ._continuation_support import configure_interactions_store, drain, make_captured, make_wired


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


@pytest.fixture
def captured(monkeypatch):
    return make_captured(monkeypatch)


def _window() -> DeadlineWindow:
    now = datetime.now(UTC)
    return DeadlineWindow(
        budget=60.0, created_at=now, timeout_at=now + timedelta(seconds=60), deadline=0.0, park_ttl_margin_seconds=10
    )


def test_build_request_records_the_chain_without_the_ask_frame() -> None:
    # The ask runs as the innermost tool dispatch; its own frame is dropped from
    # ``asked_by`` so a resume never re-adds it.
    with tool_call_frame("main"), tool_call_frame("sub"), tool_call_frame("ask"):
        assert current_call_chain() == ("main", "sub", "ask")
        request = build_request(
            interaction_id="i1",
            group="g1",
            question="?",
            fmt=AnswerFormat.TEXT,
            format_payload=None,
            on_mismatch=AnswerMismatchPolicy.RETRY,
            mismatch_notice=None,
            reply_to="reply:i1",
            window=_window(),
            sensitive=False,
            channel=None,
            recipient=None,
            audience=None,
            stored_media=None,
            park_binding=AsyncParkBinding(),
            mode="sync",
            expiry_at=None,
            to="user",
            payload=None,
            on_expiry="kill",
        )
    assert request.asked_by == ["main", "sub"]


def test_build_request_outside_any_frame_records_an_empty_chain() -> None:
    request = build_request(
        interaction_id="i2",
        group="g1",
        question="?",
        fmt=AnswerFormat.TEXT,
        format_payload=None,
        on_mismatch=AnswerMismatchPolicy.RETRY,
        mismatch_notice=None,
        reply_to="reply:i2",
        window=_window(),
        sensitive=False,
        channel=None,
        recipient=None,
        audience=None,
        stored_media=None,
        park_binding=AsyncParkBinding(),
        mode="sync",
        expiry_at=None,
        to="user",
        payload=None,
        on_expiry="kill",
    )
    assert request.asked_by == []


async def test_dispatch_scope_forwards_continues_chain_to_the_frame() -> None:
    # ``continues_chain`` takes the SET branch: the restored chain becomes the call
    # chain and ``key`` is NOT pushed.
    fake_app = cast(
        "TaiMCP",
        SimpleNamespace(
            preset_manager=SimpleNamespace(is_registered=lambda _key: False, active_version=lambda _key: None)
        ),
    )
    async with dispatch_scope(fake_app, "resume_tool", continues_chain=["main", "sub"]):
        assert current_call_chain() == ("main", "sub")
    # Without the keyword the frame PUSHES ``key``.
    async with dispatch_scope(fake_app, "plain_tool"):
        assert current_call_chain() == ("plain_tool",)
    assert current_call_chain() == ()


async def test_run_continuation_passes_the_chain_as_continues_chain(monkeypatch):
    # The detached resume re-enters through ``run_tool`` with the parked run's chain as
    # ``continues_chain`` — so the resumed run restores that chain and the continuation
    # tool's own name is not pushed.
    from contextlib import asynccontextmanager

    from tai42_contract.app import tai42_app

    seen: dict[str, object] = {}

    @asynccontextmanager
    async def _fake_bind(identity, *, bound_fingerprint=""):
        yield

    monkeypatch.setattr(continuation_module, "bind_execution_identity", _fake_bind)

    class _Tools:
        async def run_tool(self, key, arguments, *, offload_sync=False, continues_chain=None):
            seen["key"] = key
            seen["continues_chain"] = continues_chain
            return None

    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=_Tools()))

    await continuation_module._run_continuation("id-1", None, "resume_tool", "iid-1", {"a": 1}, None, ("main", "sub"))
    assert seen["key"] == "resume_tool"
    assert seen["continues_chain"] == ("main", "sub")


async def test_dispatch_continuation_forwards_request_asked_by(wired, captured):
    # The async request's ``asked_by`` reaches the detached ``_run_continuation``.
    now = datetime.now(UTC)
    request = InteractionRequest(
        interaction_id="dc1",
        group_id="g1",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("dc1"),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=now + timedelta(seconds=60),
        asked_by=["main", "sub"],
    )
    continuation_module.dispatch_continuation(wired.store, request, "fp-1", "go")
    await drain()
    assert len(captured) == 1
    assert captured[0]["park_asked_by"] == ["main", "sub"]


async def test_redeliver_continuation_forwards_due_asked_by(wired, captured):
    # The reaper redelivery path reads the chain off the durable due-record.
    due = ContinuationDue(
        interaction_id="rd1",
        tool="resume_tool",
        identity="svc-key",
        fingerprint="fp-1",
        answer="go",
        attempts=1,
        asked_by=["main", "sub"],
    )
    continuation_module.redeliver_continuation(wired.store, due)
    await drain()
    assert len(captured) == 1
    assert captured[0]["park_asked_by"] == ["main", "sub"]


def test_continuation_due_mapping_carries_asked_by_only_when_present() -> None:
    with_chain = records._continuation_due_mapping("resume_tool", "svc-key", "fp-1", "go", None, ["main", "sub"])
    assert json.loads(with_chain["asked_by"]) == ["main", "sub"]
    # An empty / omitted chain adds no field (the record stays minimal).
    without = records._continuation_due_mapping("resume_tool", "svc-key", "fp-1", "go")
    assert "asked_by" not in without
    empty = records._continuation_due_mapping("resume_tool", "svc-key", "fp-1", "go", None, [])
    assert "asked_by" not in empty


def test_continuation_due_defaults_to_an_empty_chain() -> None:
    due = ContinuationDue(interaction_id="x", tool="t", identity="i", fingerprint="f", answer=None, attempts=0)
    assert due.asked_by == []


def test_async_request_round_trips_asked_by_on_the_model() -> None:
    # The field survives model serialization (durable JSON round-trip).
    now = datetime.now(UTC)
    original = InteractionRequest(
        interaction_id="rt1",
        group_id="g1",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to="reply:rt1",
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
        asked_by=["a", "b"],
    )
    restored = InteractionRequest.model_validate_json(original.model_dump_json())
    assert restored.asked_by == ["a", "b"]
