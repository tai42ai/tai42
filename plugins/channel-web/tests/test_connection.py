"""The store connection — the fail-closed redis guard fires for every store op, the
shared-default fallback, and the timeout-stripped tail connection."""

from __future__ import annotations

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_channel_web.store import connection
from tai42_channel_web.store.questions import (
    QuestionRecord,
    claim_question,
    release_question,
    reserve_question,
    restore_question,
)
from tai42_channel_web.store.registrations import drop_session, register_session, resolve_session
from tai42_channel_web.store.transcript import append_answered, append_media, append_message, append_question

from .conftest import CALLBACK, IDENTITY, SESSION_TOKEN, VISITOR_ID, FakeRedis, _deadline

pytestmark = pytest.mark.usefixtures("web_env")


async def test_every_store_op_raises_when_redis_url_unset(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHANNEL_WEB_REDIS_URL")
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()

    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline())
    for call in (
        register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {}),
        resolve_session(SESSION_TOKEN),
        drop_session(SESSION_TOKEN),
        append_message(IDENTITY, VISITOR_ID, "in", "x"),
        append_media(IDENTITY, VISITOR_ID, "x", None, None),
        append_question(IDENTITY, VISITOR_ID, "int-1", "q", "text", None, _deadline()),
        append_answered(IDENTITY, VISITOR_ID, "int-1", "a"),
        reserve_question("int-1", record),
        release_question("int-1"),
        claim_question("int-1"),
        restore_question("int-1", record, 5),
    ):
        with pytest.raises(ValueError, match="CHANNEL_WEB_REDIS_URL"):
            await call


def test_redis_guard_passes_when_set():
    assert connection._redis_settings().redis_url == "redis://test/0"


def test_redis_url_falls_back_to_the_default_namespace(monkeypatch: pytest.MonkeyPatch):
    # The store connection falls back to the shared TAI_DEFAULT_REDIS_URL when the
    # channel's own url is unset; the guard fires only when NEITHER is set.
    monkeypatch.delenv("CHANNEL_WEB_REDIS_URL")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared/1")
    reset_all_settings()
    assert connection._redis_settings().redis_url == "redis://shared/1"


def test_tail_connection_strips_the_socket_read_timeout(stub_app, fake_redis: FakeRedis):
    # The keepalive XREAD blocks legitimately; a blanket read timeout would kill it.
    connection.tail_redis_ctx()
    kwargs = stub_app.clients.ctx_kwargs[-1]
    assert kwargs["settings"].socket_timeout is None
    assert kwargs["fresh"] is True
