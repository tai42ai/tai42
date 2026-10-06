"""Every FEATURE's model call records under the run's trace through the ONE seam.

One case per agent kind drives the agent's real face against the recording monitoring
backend bound in ``conftest.py`` (whose ``get_monitoring_callbacks`` returns a real
handler recording the trace id each model call fires under) and a scripted real chat
model. Each case asserts the recording handler saw at least one ``on_chat_model_start``
bound to the run's resolved trace id — so the per-invoke monitoring config reached the
model call. The retrieval finalization and the vqa structured path are the paths that
carried no config before the seam existed.

The voting and refine cases also assert ONE trace per run: every sub-run (voters + judge;
evaluator + critic + final pass) resolves the SAME trace id rather than minting N roots.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.agent.events import StreamEvent
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._deep_agent_fakes import _FakeCompiledGraph, _install_fake_resolve
from tests._retrieval_tools_agent_support import StubStore

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal.base_tool_agent import ainvoke_tools_agent
from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.refine_agent import agent as refine_mod
from tai42_agents.refine_agent.agent import RefineAgent
from tai42_agents.refine_agent.prompt import CRITIC_APPROVAL_MESSAGE
from tai42_agents.retrieval_tools_agent import agent as ragent
from tai42_agents.retrieval_tools_agent.agent import RetrievalToolsAgent
from tai42_agents.voting_agent import agent as voting_mod
from tai42_agents.voting_agent.agent import VotingAgent
from tai42_agents.voting_agent.model import VoterSpec
from tai42_agents.vqa_agent import VqaAgent

from .conftest import RecordingMonitoringWriter


class _RecordingChatModel(BaseChatModel):
    """A real chat model that replays scripted messages and fires ``on_chat_model_start``.

    Being a genuine ``BaseChatModel``, the LangChain callback machinery fires
    ``on_chat_model_start`` on the monitoring handlers riding the run config — the behavior
    the per-invoke seam exists to deliver. ``bind_tools`` is a no-op so an agent can bind
    its tools and still be scripted. ``profile`` opts the model into the kit's native
    structured-output path when a case needs it.
    """

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(self, responses: Sequence[BaseMessage], profile: dict[str, Any] | None = None, **kwargs: Any) -> None:
        # ``profile`` is the base ``BaseChatModel`` capability field the kit's structured
        # plan reads to choose native vs tool structured output; a dict stands in for it.
        super().__init__(profile=cast(Any, profile), **kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        message = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _writer() -> RecordingMonitoringWriter:
    return cast(RecordingMonitoringWriter, tai42_app.monitoring.active.writer)


@pytest.fixture(autouse=True)
def _reset_recording() -> None:
    writer = _writer()
    writer.contexts.clear()
    writer.chat_model_starts.clear()


def _run_trace_ids() -> set[str | None]:
    """The distinct trace ids the active backend was asked for across this run."""
    return {ctx.trace_id for ctx in _writer().contexts}


def _assert_model_call_recorded_under_the_run_trace() -> None:
    writer = _writer()
    assert writer.chat_model_starts, "no model call fired on_chat_model_start under the run"
    assert set(writer.chat_model_starts) <= _run_trace_ids(), (
        "a model call fired under a trace id the seam never resolved for this run"
    )


async def _collect(stream: Any) -> list[StreamEvent]:
    return [event async for event in stream]


# --- tools_agent ----------------------------------------------------------------------


def _patch_tools_seams(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel) -> None:
    saver = InMemorySaver()
    monkeypatch.setattr(
        bta, "llm_provider_settings", lambda: SimpleNamespace(llm="p", checkpoint="c", checkpoint_conn_string=None)
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))
    monkeypatch.setattr(bta, "context_overflow_middlewares", lambda **_: _empty_middlewares())

    async def fake_get_llm(*, provider: str, **_kwargs: Any) -> BaseChatModel:
        return model

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    monkeypatch.setattr(bta, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))


async def _empty_middlewares() -> list[Any]:
    return []


def test_tools_agent_model_call_records_under_the_run_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_tools_seams(monkeypatch, _RecordingChatModel([AIMessage(content="done")]))
    asyncio.run(ainvoke_tools_agent(system_message="sys", user_message=["hi"], tools=[]))
    _assert_model_call_recorded_under_the_run_trace()


# --- voting_agent ---------------------------------------------------------------------


def test_voting_agent_records_every_sub_run_under_one_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    # One voter plus the judge each drive a tools-agent graph; all nest under ONE run trace.
    _patch_tools_seams(monkeypatch, _RecordingChatModel([AIMessage(content="verdict")]))
    monkeypatch.setattr(voting_mod, "llm_provider_settings", lambda: SimpleNamespace(llm="p"))

    asyncio.run(
        _collect(
            VotingAgent().astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="vote"),
                voters=[VoterSpec(provider="p")],
            )
        )
    )
    _assert_model_call_recorded_under_the_run_trace()
    # ONE trace per run: the voter and the judge resolved the same trace id, not two roots.
    assert len(_run_trace_ids()) == 1
    assert len(_writer().chat_model_starts) >= 2


# --- refine_agent ---------------------------------------------------------------------


def test_refine_agent_records_every_sub_run_under_one_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    # Evaluator drafts, critic approves on the first pass, the final pass re-runs the
    # evaluator — three model calls, all nesting under ONE run trace.
    model = _RecordingChatModel(
        [AIMessage(content="draft"), AIMessage(content=CRITIC_APPROVAL_MESSAGE), AIMessage(content="final")]
    )
    saver = InMemorySaver()
    monkeypatch.setattr(
        refine_mod,
        "llm_provider_settings",
        lambda: SimpleNamespace(llm="p", checkpoint="c", checkpoint_conn_string=None),
    )
    monkeypatch.setattr(refine_mod, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))
    monkeypatch.setattr(refine_mod, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: False))
    monkeypatch.setattr(refine_mod, "context_overflow_middlewares", lambda **_: _empty_middlewares())

    async def fake_get_llm(*, provider: str, **_kwargs: Any) -> BaseChatModel:
        return model

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    monkeypatch.setattr(refine_mod, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(
        refine_mod, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer)
    )

    asyncio.run(
        _collect(
            RefineAgent().astream(
                evaluator_message=TemplatedText(content="write"),
                critic_message=TemplatedText(content="review"),
                max_iterations=3,
            )
        )
    )
    _assert_model_call_recorded_under_the_run_trace()
    assert len(_run_trace_ids()) == 1
    assert len(_writer().chat_model_starts) >= 2


# --- retrieval_tools_agent ------------------------------------------------------------

_RETRIEVAL_SCHEMA = {"title": "Answer", "type": "object", "properties": {"answer": {"type": "string"}}}


def test_retrieval_tools_agent_graph_and_finalization_record_under_the_run_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The graph's model turn emits the terminal status envelope; the structured
    # finalization (a direct model call) forces the schema — both must record under the run.
    model = _RecordingChatModel(
        [
            AIMessage(content='{"status": "success", "message": "m", "result": "the answer"}'),
            AIMessage(content='{"answer": "the answer"}'),
        ],
        profile={"structured_output": True},
    )
    saver = InMemorySaver()
    provider_settings = SimpleNamespace(
        llm="openai",
        embedding="emb",
        checkpoint="ckpt",
        store="store",
        store_conn_string=None,
        checkpoint_conn_string=None,
    )
    monkeypatch.setattr(ragent, "llm_provider_settings", lambda: provider_settings)
    monkeypatch.setattr(ragent, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))
    monkeypatch.setattr(
        ragent, "embedding_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs))
    )

    async def fake_resolve_tools(app_tools: Any, names: list[str], tools: list[Any], presets: list[Any]) -> list[Any]:
        return []

    monkeypatch.setattr(ragent, "resolve_tools", fake_resolve_tools)

    async def fake_get_llm(*, provider: str, **_kwargs: Any) -> BaseChatModel:
        return model

    monkeypatch.setattr(ragent, "get_llm_async", fake_get_llm)

    embedding = SimpleNamespace(aembed_query=_fake_embed)

    async def fake_get_embedding(*, provider: str, **_kwargs: Any) -> Any:
        return embedding

    monkeypatch.setattr(ragent, "get_embedding_async", fake_get_embedding)

    async def fake_get_store(*, provider: str, conn_string: Any, **_kwargs: Any) -> Any:
        return StubStore([])

    monkeypatch.setattr(ragent, "store_registry", lambda: SimpleNamespace(get_store=fake_get_store))

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    monkeypatch.setattr(ragent, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))

    events = asyncio.run(
        _collect(
            RetrievalToolsAgent().astream(user_message=TemplatedText(content="hi"), response_format=_RETRIEVAL_SCHEMA)
        )
    )
    assert events  # a structured terminal was produced
    _assert_model_call_recorded_under_the_run_trace()
    # Two model calls recorded: the graph turn and the finalization, both under the run trace.
    assert len(_writer().chat_model_starts) >= 2
    assert len(_run_trace_ids()) == 1


async def _fake_embed(_text: str) -> list[float]:
    return [0.0] * 6


# --- vqa_agent ------------------------------------------------------------------------


def test_vqa_agent_text_path_records_under_the_run_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _RecordingChatModel([AIMessage(content="a cat")])
    _patch_vqa(monkeypatch, model, provider="p")
    asyncio.run(_collect(VqaAgent().astream(image_url="http://img", query="describe")))
    _assert_model_call_recorded_under_the_run_trace()


def test_vqa_agent_structured_path_records_under_the_run_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _RecordingChatModel([AIMessage(content='{"answer": "a cat"}')], profile={"structured_output": True})
    _patch_vqa(monkeypatch, model, provider="openai")
    asyncio.run(
        _collect(VqaAgent().astream(image_url="http://img", query="describe", response_format=_RETRIEVAL_SCHEMA))
    )
    _assert_model_call_recorded_under_the_run_trace()


def _patch_vqa(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, provider: str) -> None:
    from tai42_agents import vqa_agent as vqa

    async def fake_get_llm(provider: str, **_kwargs: Any) -> BaseChatModel:
        return model

    monkeypatch.setattr(vqa, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(vqa, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: {}))
    monkeypatch.setattr(vqa, "llm_provider_settings", lambda: SimpleNamespace(llm=provider))


# --- langchain_deep_agent -------------------------------------------------------------


class _CallbackFiringGraph(_FakeCompiledGraph):
    """A scripted compiled graph that fires ``on_chat_model_start`` from the run config.

    A real deepagents graph propagates the run config's callbacks to its model node (the
    langgraph ``var_child_runnable_config`` discipline). This stand-in fires the handlers
    the config carries, so the test proves the deep agent handed the per-invoke monitoring
    config to the graph its model node runs under.
    """

    async def astream(self, agent_input: Any, config: Any, stream_mode: Any = None) -> Any:
        self.received_input = agent_input
        self.received_config = config
        for handler in (config or {}).get("callbacks", []):
            if hasattr(handler, "on_chat_model_start"):
                handler.on_chat_model_start({}, [])
        for chunk in self._chunks:
            yield chunk


def test_langchain_deep_agent_model_call_records_under_the_run_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fake only the graph build so the REAL run-config seam still runs: the config the
    # deep agent hands to its graph (where the model node runs) carries the run's callbacks.
    agent = DeepAgent()
    graph = _CallbackFiringGraph([("updates", {"agent": {"messages": [AIMessage(content="done")]}})], interrupts=[])
    _install_fake_resolve(monkeypatch, agent, graph)
    asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="go"), thread_id="t")))
    _assert_model_call_recorded_under_the_run_trace()
