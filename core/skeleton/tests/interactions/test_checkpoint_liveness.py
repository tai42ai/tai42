"""The interactions live-thread filter: a checkpoint thread that backs a live async park is claimed."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from tai42_kit.llm.checkpoint import live_thread_filters

from tai42_skeleton.interactions import checkpoint_liveness


class _FakeParkStore:
    """A thread reverse index and a thread-subject index over fixed members."""

    def __init__(self, reverse: set[str], subject: set[str]) -> None:
        self._reverse = reverse
        self._subject = subject

    async def thread_park_members(self, conn: object, thread_id: str) -> list[str]:
        return ["i-reverse"] if thread_id in self._reverse else []

    async def subject_members(self, conn: object, kind: str, key: str) -> list[str]:
        assert kind == "thread"
        return ["i-subject"] if key in self._subject else []


def _install(monkeypatch: pytest.MonkeyPatch, *, reverse: set[str], subject: set[str], configured: bool = True) -> None:
    monkeypatch.setattr(
        checkpoint_liveness,
        "interactions_settings",
        lambda: SimpleNamespace(redis=SimpleNamespace(redis_url="redis://x" if configured else None), key_prefix="ix:"),
    )
    monkeypatch.setattr(checkpoint_liveness, "InteractionStore", lambda prefix: _FakeParkStore(reverse, subject))

    @asynccontextmanager
    async def _ctx(cls: object, settings: object):
        yield object()

    monkeypatch.setattr(checkpoint_liveness, "client_ctx", _ctx)


def test_registered_under_interactions():
    assert live_thread_filters()["interactions"] is checkpoint_liveness.threads_with_live_parks


async def test_a_thread_with_a_reverse_index_or_subject_member_is_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, reverse={"t-reverse"}, subject={"t-subject"})
    claimed = await checkpoint_liveness.threads_with_live_parks("postgres", None, ["t-reverse", "t-subject", "t-idle"])
    assert claimed == {"t-reverse", "t-subject"}


async def test_an_unconfigured_interactions_store_claims_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, reverse={"t"}, subject=set(), configured=False)
    assert await checkpoint_liveness.threads_with_live_parks("redis", None, ["t"]) == set()
