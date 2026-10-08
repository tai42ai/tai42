"""The agent async-park resume driver: the ``agent_resume`` continuation, the drive lease and
super-step barrier, the ``finalize_drive`` raise branch, and the full cross-worker cycle.

A real ``DeepAgent.run`` parks and returns a suspended receipt, then ``agent_resume`` rebuilds
the graph on a fresh runtime (same checkpointer + the durable park index) and drives it to
completion, running the parked tool exactly once. The agents' park index is bound to an in-memory
fakeredis; the checkpoint is an
``InMemorySaver`` shared between the park run and the resume, standing in for the durable
checkpoint a cross-worker resume reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    ResumeBuffered,
    RunFailed,
    RunTerminalFailed,
    SuspendedInteraction,
    get_resume_continuation_tool,
    suspended_interaction_marker,
)
from tai42_contract.template import TemplatedText
from tai42_kit.interactions.park_index import DriveInProgressError, superstep_id
from tests.conftest import bind_park_index

from tai42_agents._internal.park import agent_resume, finalize_drive
from tai42_agents._internal.park import capability as cap
from tai42_agents._internal.park.errors import (
    AgentParkNotHostableError,
    AgentResumeParkEntryNotFoundError,
)
from tai42_agents._internal.park.park_binding import agents_park_index
from tai42_agents.langchain_deep_agent import agent as agent_mod
from tai42_agents.langchain_deep_agent import run_thread as run_thread_mod
from tai42_agents.langchain_deep_agent.tool_spec import DeepSubAgentSpec

from .conftest import fake_run_trace


class ScriptedChatModel(BaseChatModel):
    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        message = self._responses[self._index]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


# A park deadline comfortably WITHIN the durable-workspace retention horizon (session_ttl,
# 24h by default): the deep agent's run now acquires a persistent workspace whose idle-reap TTL
# bounds the park's retention to min(checkpoint, workspace), so a full-run park must
# carry an ask deadline within it — a None-deadline ("wait forever") park is now correctly
# refused because the workspace would reap first.
_WITHIN_HORIZON = datetime.now(UTC) + timedelta(hours=1)


class _CountingAsk:
    def __init__(self, interaction_id: str, expiry_at: datetime | None = _WITHIN_HORIZON) -> None:
        self.calls = 0
        self._interaction_id = interaction_id
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            self.calls += 1
            return suspended_interaction_marker(self._interaction_id, self._expiry_at, get_resume_continuation_tool())

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _ask_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "ask", "args": {}}])


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the park index at a shared in-memory fakeredis and report the park Redis as
    configured (so a run is judged park-capable)."""
    redis = aioredis.FakeRedis(decode_responses=True)
    bind_park_index(monkeypatch, redis)
    return redis


# ---- agent_resume driver --------------------------------------------------


async def _write_park(entry_ids: list[str], interrupt_id: str = "int1", thread_id: str = "t") -> str:
    step = superstep_id(entry_ids)
    entries = {
        interaction_id: {
            "agent_name": "langchain_deep_agent",
            "thread_id": thread_id,
            "superstep_id": step,
            "interrupt_id": interrupt_id,
            # Engine facts (checkpoint provider, recursion limit) ride inside rebuild_kwargs,
            # never top-level entry fields, on the provider-free index.
            "rebuild_kwargs": {"checkpoint_provider": "redis", "recursion_limit": 50},
            # An unchained park: no cross-driver chain routing captured, so its terminal fires
            # nothing and the platform delivers to the run's own address.
            "completion_tool": None,
            "completion_context": None,
        }
        for interaction_id in entry_ids
    }
    await agents_park_index().persist(
        thread_id=thread_id,
        superstep=step,
        entries=entries,
        expected=dict.fromkeys(entry_ids),
        entry_ttl=dict.fromkeys(entry_ids, 100),
        barrier_ttl=100,
    )
    return step


async def _seed_finalized(thread_id: str, step: str, ids: list[str], *, resolution: Any, value: Any) -> None:
    """Claim the drive lease and finalize the super-step under it, as the drive and the kill both do."""
    index = agents_park_index()
    async with index.claim(thread_id, step) as lease:
        await index.finalize(lease, member_ids=ids, resolution=resolution, value=value)


def test_agent_resume_buffers_until_all_siblings_answered(fake_park_redis: Any) -> None:
    async def go() -> None:
        await _write_park(["iA", "iB"])
        out = await agent_resume("iA", "answer-a")
        assert out == ResumeBuffered(remaining_ids=["iB"])
        # The still-pending sibling keeps the park entries in place.
        assert await agents_park_index().read_entry("iA") is not None
        assert await agents_park_index().read_entry("iB") is not None

    asyncio.run(go())


def test_agent_resume_raises_on_lost_drive_lease(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        # Another live worker already holds the drive lease.
        await agents_park_index().claim("t", superstep_id).acquire()
        with pytest.raises(DriveInProgressError):
            await agent_resume("i1", "the answer")
        # The park index is LEFT intact so the platform's reaper redelivers.
        assert await agents_park_index().read_entry("i1") is not None

    asyncio.run(go())


def test_agent_resume_raises_on_missing_park_entry(fake_park_redis: Any) -> None:
    async def go() -> None:
        with pytest.raises(AgentResumeParkEntryNotFoundError):
            await agent_resume("nope", "x")

    asyncio.run(go())


# ---- finalize_drive raise branch ----------------------------------------------------------


def test_finalize_drive_raises_on_a_park_with_no_identity_bound(fake_park_redis: Any) -> None:
    from tai42_agents._internal.park.middleware import AGENT_PARK_PAYLOAD_KEY

    class _Interrupt:
        def __init__(self, id_: str, value: Any) -> None:
            self.id = id_
            self.value = value

    class _Task:
        def __init__(self, interrupts: list[Any]) -> None:
            self.interrupts = interrupts
            self.state = None

    class _Snapshot:
        def __init__(self, tasks: list[Any]) -> None:
            self.tasks = tasks

    class _FakeAgent:
        def __init__(self, snapshot: Any) -> None:
            self._snapshot = snapshot

        async def aget_state(self, config: Any, subgraphs: bool = False) -> Any:
            return self._snapshot

    park_value = {AGENT_PARK_PAYLOAD_KEY: {"interactions": {"i1": None}}}
    agent = _FakeAgent(_Snapshot([_Task([_Interrupt("int1", park_value)])]))

    async def go() -> None:
        # A park interrupt surfaced but no park identity is bound (interrupt_on forces the state
        # read): the run parked with no durable resume path, so finalize raises loudly.
        with pytest.raises(AgentParkNotHostableError, match="no park identity bound"):
            await finalize_drive(agent, {}, {"approve": True}, None)

    asyncio.run(go())


def test_agent_resume_on_resolved_tombstone_replays_the_stored_terminal(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        # The super-step already drove cleanly: its entry is a resolved tombstone plus a resolution
        # record holding the terminal outcome.
        await _seed_finalized("t", superstep_id, ["i1"], resolution="terminal", value="all done")
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert _is_resolved(entry)
        # The tombstone carries the coordinates that LOCATE the resolution record.
        assert agents_park_index().tombstone_coordinates(entry) == ("t", superstep_id)
        # A lapped redelivery of an orphaned due-record REPLAYS the stored terminal — no raise, no
        # re-drive; the platform re-runs its idempotent ladder.
        out = await agent_resume("i1", "the answer")
        assert out == "all done"

    asyncio.run(go())


def test_agent_resume_on_aborted_tombstone_replays_the_aborted_run_failed(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        aborted = RunFailed(outcome={"status": "aborted", "reason": "killed"})
        await _seed_finalized("t", superstep_id, ["i1"], resolution="aborted", value=aborted)
        # A redrive of a killed super-step replays the aborted RunFailed (the platform face raises
        # it, so the platform delivers FAILED deduped against the kill's own FAILED).
        assert await agent_resume("i1", "the answer") == aborted

    asyncio.run(go())


def test_agent_resume_on_benign_detach_tombstone_is_a_noop(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A detached chain writes a record-LESS tombstone (no thread/superstep, no resolution
        # record), so a fire that lands on it reads no resolution and no-ops.
        await agents_park_index().detach(["chain-key"])
        entry = await agents_park_index().read_entry("chain-key")
        assert entry is not None
        assert agents_park_index().tombstone_kind(entry) == "detached"
        assert await agent_resume("chain-key", "late fire") is None

    asyncio.run(go())


def test_two_completers_race_loser_then_replays_on_redelivery(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        index = agents_park_index()
        # Worker A won the barrier and holds a live drive lease.
        worker_a = await index.claim("t", superstep_id).acquire()
        # Worker B's redelivery completes the barrier but loses the drive: it raises so the
        # platform retains its durable retry ticket.
        with pytest.raises(DriveInProgressError):
            await agent_resume("i1", "the answer")
        assert await index.read_entry("i1") is not None

        # Worker A finishes and finalizes the super-step to a tombstone + terminal resolution.
        await index.finalize(worker_a, member_ids=["i1"], resolution="terminal", value="done-by-A")
        await worker_a.close()
        # Worker B redelivers again and now replays A's terminal on the tombstone.
        out = await agent_resume("i1", "the answer")
        assert out == "done-by-A"

    asyncio.run(go())


# ---- full cross-worker cycle ----------------------------------------------


class _Registry:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def get_checkpointer(self, **kwargs: Any) -> Any:
        return self._value

    async def get_store(self, **kwargs: Any) -> Any:
        return self._value


class _ProviderSettings:
    llm = "fake"
    checkpoint = "redis"
    checkpoint_conn_string = None
    store = "memory"
    store_conn_string = None


class _LlmSettings:
    def with_fallbacks(self, kwargs: Any) -> dict[str, Any]:
        return {}


def _wire_real_build(
    monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver, store: InMemoryStore
) -> None:
    """Keep the REAL build_langchain_deep_agent but inject the scripted model + a SHARED
    checkpointer/store, so the park run and its resume read one durable checkpoint."""

    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(agent_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(agent_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(agent_mod, "store_registry", lambda: _Registry(store))
    monkeypatch.setattr(agent_mod, "llm_provider_settings", _ProviderSettings)
    # The park-persist gate reads the checkpoint retention through the capability module's own
    # ``llm_provider_settings``; point it at the same keep-forever double.
    monkeypatch.setattr(cap, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(agent_mod, "llm_settings", _LlmSettings)
    # The recording app's monitoring writer hands back string callback sentinels; keep
    # them out of the run config (the config already carries the pinned thread_id).
    monkeypatch.setattr(run_thread_mod, "init_langgraph_config", lambda config=None: fake_run_trace(config))


def test_full_park_resume_cycle_runs_ask_once_and_clears_index(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    store = InMemoryStore()
    ask = _CountingAsk("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_real_build(monkeypatch, model, saver, store)
    app_tools.client_tools["ask"] = ask.tool()

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> Any:
        receipt = await agent.run(
            tool_names=["ask"], checkpoint_provider="redis", user_message=TemplatedText(content="go"), thread_id="t-int"
        )
        assert isinstance(receipt, SuspendedInteraction)
        assert receipt.interaction_id == "i1"
        assert receipt.interaction_ids == ["i1"]
        # A user ask (the ``ask`` stand-in stamps no caller subset), so the caller partition is empty.
        assert receipt.caller_interaction_ids == []
        assert receipt.expiry_at == _WITHIN_HORIZON
        assert ask.calls == 1
        assert await agents_park_index().read_entry("i1") is not None

        # Resume on a fresh runtime: same shared saver stands in for the durable checkpoint.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        # The parked tool ran exactly once — the resume substituted the answer, never re-ran it.
        assert ask.calls == 1
        # A clean drive finalizes the park entry to a resolved tombstone (not an absent key), so
        # a lapped redelivery clears benignly instead of storming on a vanished entry.
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert _is_resolved(entry)

    asyncio.run(go())


def test_agent_resume_rejects_a_no_longer_pending_interrupt(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    store = InMemoryStore()
    ask = _CountingAsk("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_real_build(monkeypatch, model, saver, store)
    app_tools.client_tools["ask"] = ask.tool()

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> None:
        await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-stale",
        )

        # Corrupt the stored interrupt id so it no longer matches the pending park interrupt.
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        entry["interrupt_id"] = "bogus-interrupt-id"
        await agents_park_index().persist(
            thread_id=entry["thread_id"],
            superstep=entry["superstep_id"],
            entries={"i1": entry},
            expected={"i1": None},
            entry_ttl={"i1": 100},
            barrier_ttl=100,
        )

        # The resumed run's raise is its failed terminal: an unchained park is finalized with the
        # failure and the platform face raises it, so the platform delivers FAILED once.
        with pytest.raises(RunTerminalFailed) as raised:
            await agent_resume("i1", "the answer")
        assert raised.value.outcome["error_type"] == "AgentResumeInterruptNotPendingError"
        record = await agents_park_index().read_resolution(entry["thread_id"], entry["superstep_id"])
        assert record is not None
        assert record.value == RunFailed(outcome=raised.value.outcome)

    asyncio.run(go())


# ---- M=2 parallel-subagent park driven to completion through agent_resume --


class _EchoingScriptedModel(BaseChatModel):
    """Like ``ScriptedChatModel`` but a scripted response may be a callable ``(messages) ->
    AIMessage``, so a finalize turn can ECHO the answer that was substituted into the last
    tool result. This is how a resumed subagent's output is made to depend on ITS OWN
    answer, proving each answer landed on its own interrupt rather than crossing over."""

    _responses: list[BaseMessage | Callable[[list[BaseMessage]], BaseMessage]] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(
        self, responses: Sequence[BaseMessage | Callable[[list[BaseMessage]], BaseMessage]], **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "echoing-scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _EchoingScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        response = self._responses[self._index]
        self._index += 1
        message: BaseMessage = response(messages) if callable(response) else response
        return ChatResult(generations=[ChatGeneration(message=message)])


def _ask_for_subagent(messages: list[BaseMessage]) -> AIMessage:
    """A subagent's ask turn: read the task description (``A`` / ``B``) this subagent was
    launched with and pass it to ``ask``, so subagent A always parks ``iA`` and subagent B
    always parks ``iB``. This binds each subagent to a FIXED interaction independently of the
    non-deterministic order the two parallel subagents reach the model, so the terminal
    ``task_id -> answer`` mapping is stable and a crossover cannot hide behind a scheduling flip."""
    who = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
    return AIMessage(content="", tool_calls=[{"id": f"c{who}", "name": "ask", "args": {"who": who}}])


def _echo_last_tool_result(messages: list[BaseMessage]) -> AIMessage:
    """A subagent's finalize: echo the content of the last tool result — the answer just
    substituted into its own ``ask`` interrupt."""
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    return AIMessage(content=str(tool_messages[-1].content))


def _echo_task_results(messages: list[BaseMessage]) -> AIMessage:
    """The main agent's finalize: surface each subagent task's result keyed by ITS task id in a
    fixed ``ta`` then ``tb`` order, so the terminal output pins which subagent carried which
    answer. A crossover that fed one interrupt the other's answer would swap the two task
    results and flip the output, so the per-task assertion fails — proving both designated
    answers arrived, each exactly once, with no crossover between the interrupts."""
    by_task = {
        m.tool_call_id: str(m.content)
        for m in messages
        if isinstance(m, ToolMessage) and m.tool_call_id in {"ta", "tb"}
    }
    return AIMessage(content=f"ta={by_task['ta']};tb={by_task['tb']}")


def _two_parallel_subagent_park_setup(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> tuple[Any, Any, dict[str, int]]:
    """Wire (no async yet) a REAL deep-agent run whose main agent calls one ``asker`` subagent
    twice in parallel; each subagent async-asks and parks, surfacing two distinct interrupts in
    one super-step. Subagent A (task ``ta``, description ``A``) parks ``iA`` and subagent B
    (task ``tb``, description ``B``) parks ``iB`` — a FIXED binding, so ``ta`` always carries
    iA's answer and ``tb`` iB's regardless of parallel scheduling order. Each answer is
    self-identifying (``for-iA`` / ``for-iB``) and finalize turns echo the answer their own
    ``ask`` interrupt received, so the terminal output carries both designated answers only
    when each reached the interrupt it was addressed to."""
    saver = InMemorySaver()
    store = InMemoryStore()
    ask_calls = {"n": 0}
    id_for_who = {"A": "iA", "B": "iB"}

    def ask(who: str) -> dict[str, Any]:
        ask_calls["n"] += 1
        return suspended_interaction_marker(id_for_who[who], _WITHIN_HORIZON, get_resume_continuation_tool())

    app_tools.client_tools["ask"] = StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")

    model = _EchoingScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "ta", "name": "task", "args": {"description": "A", "subagent_type": "asker"}},
                    {"id": "tb", "name": "task", "args": {"description": "B", "subagent_type": "asker"}},
                ],
            ),
            _ask_for_subagent,  # a subagent's ask turn — parks iA or iB by its own description
            _ask_for_subagent,  # the other subagent's ask turn
            _echo_last_tool_result,  # subagent that resumes first finalizes, echoing its own answer
            _echo_last_tool_result,  # the other subagent finalizes, echoing its own answer
            _echo_task_results,  # main agent finalizes over both task results
        ]
    )
    _wire_real_build(monkeypatch, model, saver, store)

    subagent = DeepSubAgentSpec(
        name="asker", description="asks the user", system_prompt=TemplatedText(content="ask"), tools=["ask"]
    )
    agent = tai42_app.agents.get_agent("langchain_deep_agent")
    assert isinstance(agent, agent_mod.DeepAgent)
    return agent, subagent, ask_calls


async def _park_two_parallel_subagents(agent: Any, subagent: Any, ask_calls: dict[str, int], thread_id: str) -> None:
    """Drive the parking turn: the main agent calls the ``asker`` subagent twice in parallel,
    each async-asks and parks, surfacing two interrupts in one super-step. The whole cycle
    (this park plus the caller's resume) runs in ONE event loop so the shared fakeredis
    connection never crosses loops."""
    receipt = await agent.run(
        subagents=[subagent],
        checkpoint_provider="redis",
        user_message=TemplatedText(content="go"),
        thread_id=thread_id,
    )
    assert isinstance(receipt, SuspendedInteraction)
    assert set(receipt.interaction_ids) == {"iA", "iB"}
    # Each subagent's ask ran exactly once — two parks, one interaction apiece.
    assert ask_calls["n"] == 2
    assert await agents_park_index().read_entry("iA") is not None
    assert await agents_park_index().read_entry("iB") is not None


def test_two_parallel_subagent_parks_resume_iA_first(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    thread_id = "t-multipark-a"
    agent, subagent, ask_calls = _two_parallel_subagent_park_setup(monkeypatch, app_tools)

    async def go() -> None:
        await _park_two_parallel_subagents(agent, subagent, ask_calls, thread_id)
        # Answer iA first: it buffers, no drive yet (its sibling is still outstanding).
        assert await agent_resume("iA", "for-iA") == ResumeBuffered(remaining_ids=["iB"])
        # Answering iB completes the barrier and drives the whole super-step to ONE terminal.
        result = await agent_resume("iB", "for-iB")
        # Each task echoed its OWN subagent's answer — ``ta`` carried iA's, ``tb`` carried iB's.
        # A swap would flip these, so this pins answer→interrupt routing: no crossover.
        assert result == "ta=for-iA;tb=for-iB"
        # No ask re-ran on resume — the answers were substituted, never re-invoked.
        assert ask_calls["n"] == 2
        # Both entries tombstoned in one finalize; the barrier is gone (single terminal).
        for interaction_id in ("iA", "iB"):
            entry = await agents_park_index().read_entry(interaction_id)
            assert entry is not None
            assert _is_resolved(entry)
        assert await agents_park_index().read_barrier(thread_id, superstep_id(["iA", "iB"])) is None

    asyncio.run(go())


def test_two_parallel_subagent_parks_resume_iB_first(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    thread_id = "t-multipark-b"
    agent, subagent, ask_calls = _two_parallel_subagent_park_setup(monkeypatch, app_tools)

    async def go() -> None:
        await _park_two_parallel_subagents(agent, subagent, ask_calls, thread_id)
        # Reverse the answer order: iB first buffers, iA completes the barrier and drives.
        assert await agent_resume("iB", "for-iB") == ResumeBuffered(remaining_ids=["iA"])
        result = await agent_resume("iA", "for-iA")
        # Answer order does not change routing — ``ta`` still carries iA's answer and ``tb`` iB's;
        # each answer reached its own interrupt.
        assert result == "ta=for-iA;tb=for-iB"
        assert ask_calls["n"] == 2
        for interaction_id in ("iA", "iB"):
            entry = await agents_park_index().read_entry(interaction_id)
            assert entry is not None
            assert _is_resolved(entry)
        assert await agents_park_index().read_barrier(thread_id, superstep_id(["iA", "iB"])) is None

    asyncio.run(go())


def _is_resolved(entry: Any) -> bool:
    return agents_park_index().tombstone_kind(entry) == "resolved"
