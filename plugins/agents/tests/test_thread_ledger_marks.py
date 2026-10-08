"""The checkpoint finished-thread ledger marks an agent run makes on its own thread.

A keyless run mints its thread and marks it finished when it ends without a park, in the ledger of
the run's own checkpoint provider; a run that parks leaves its minted thread unmarked (its resume
runs under the stored id). A caller-supplied thread is the caller's: it is never marked, and it
leaves the ledger before the graph runs.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.template import TemplatedText
from tests._tools_agent_park_support import (
    ScriptedChatModel,
    _agent,
    _ask_call,
    _AskStandIn,
    _wire_tools_build,
)

from tai42_agents._internal.park import capability as park_capability
from tai42_agents._internal.park import index as idx


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_park_client() -> AsyncIterator[Any]:
        yield redis

    settings = SimpleNamespace(redis_url="redis://fake")
    monkeypatch.setattr(idx, "_park_client", fake_park_client)
    monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: settings)
    monkeypatch.setattr(park_capability, "agents_park_redis_settings", lambda: settings)
    return redis


class _LedgerWatchingModel(ScriptedChatModel):
    """Records, at its first call, the finished threads of one ledger — what the run saw when its graph ran."""

    _watch: dict[str, Any] = PrivateAttr(default_factory=dict)

    def __init__(self, responses: Any, ledger_book: Any, provider: str) -> None:
        super().__init__(responses)
        self._watch = {"book": ledger_book, "provider": provider, "seen": None}

    @property
    def finished_when_called(self) -> list[str] | None:
        return self._watch["seen"]

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        if self._watch["seen"] is None:
            ledger = self._watch["book"].ledgers.get((self._watch["provider"], None))
            self._watch["seen"] = sorted(ledger._finished) if ledger is not None else []
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_a_keyless_run_marks_its_minted_thread_in_its_own_provider_ledger(
    monkeypatch: pytest.MonkeyPatch, ledger_book: Any
) -> None:
    _wire_tools_build(monkeypatch, ScriptedChatModel([AIMessage(content="done")]), InMemorySaver())
    result = asyncio.run(_agent().run(checkpoint_provider="postgres", user_message=TemplatedText(content="go")))
    assert result == "done"
    # The test run builder mints the fixed thread id "t" for a keyless run.
    assert ledger_book.finished("postgres") == ["t"]
    assert ledger_book.finished("redis") == []


def test_a_keyless_run_on_the_default_provider_marks_the_deployment_ledger(
    monkeypatch: pytest.MonkeyPatch, ledger_book: Any
) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "postgres")
    reset_all_settings()
    _wire_tools_build(monkeypatch, ScriptedChatModel([AIMessage(content="done")]), InMemorySaver())
    try:
        asyncio.run(_agent().run(user_message=TemplatedText(content="go")))
    finally:
        reset_all_settings()
    assert ledger_book.finished("postgres") == ["t"]


def test_a_keyless_streamed_run_marks_its_minted_thread(monkeypatch: pytest.MonkeyPatch, ledger_book: Any) -> None:
    _wire_tools_build(monkeypatch, ScriptedChatModel([AIMessage(content="done")]), InMemorySaver())

    async def go() -> None:
        async for _ in _agent().astream(checkpoint_provider="postgres", user_message=TemplatedText(content="go")):
            pass

    asyncio.run(go())
    assert ledger_book.finished("postgres") == ["t"]


def test_a_keyless_run_that_parks_marks_nothing(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, ledger_book: Any, app_tools: Any
) -> None:
    ask = _AskStandIn("i1")
    _wire_tools_build(monkeypatch, ScriptedChatModel([_ask_call(), AIMessage(content="unreached")]), InMemorySaver())
    app_tools.client_tools["ask"] = ask.tool()
    receipt = asyncio.run(
        _agent().run(tool_names=["ask"], checkpoint_provider="redis", user_message=TemplatedText(content="go"))
    )
    assert isinstance(receipt, SuspendedInteraction)
    assert ledger_book.finished("redis") == []


def test_a_caller_thread_leaves_the_ledger_before_the_graph_runs_and_is_never_marked(
    monkeypatch: pytest.MonkeyPatch, ledger_book: Any
) -> None:
    model = _LedgerWatchingModel([AIMessage(content="done")], ledger_book, "postgres")
    _wire_tools_build(monkeypatch, model, InMemorySaver())

    async def go() -> Any:
        ledger = await ledger_book.ledger("postgres", None)
        await ledger.mark(["t-caller", "other"], datetime.now(UTC))
        return await _agent().run(
            checkpoint_provider="postgres", user_message=TemplatedText(content="go"), thread_id="t-caller"
        )

    assert asyncio.run(go()) == "done"
    assert model.finished_when_called == ["other"]
    assert ledger_book.finished("postgres") == ["other"]
