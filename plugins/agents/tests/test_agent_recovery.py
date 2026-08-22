"""Tool-error recovery wired into the three agents that build their own graphs
outside the tools-agent choke point: ``langchain_deep_agent``, ``refine_agent``, and
``retrieval_tools_agent``.

Each agent gets the SAME two mechanisms as the tools agent, from the one shared
owner (:mod:`tai42_agents._internal.recovery`): a turn-start repair of a thread's
dangling tool_calls attached at the single spot all its faces run through, and the
``ToolErrorToMessageMiddleware`` wired into its tool node. The tests drive each
agent's production face (or its real graph) with a scripted fake chat model and an
in-memory checkpointer — no LLM, no network.

Async code is driven with ``asyncio.run`` (the suite does not use pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, ToolException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import START
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr
from tai42_contract.agent import ToolResultStep
from tai42_contract.app import tai42_app

from tai42_agents._internal import recovery as rec
from tai42_agents._internal.stream_events import aproject_agent_events

_RECOVERY_LOGGER = "tai42_agents._internal.recovery"


class ScriptedChatModel(BaseChatModel):
    """Returns a fixed list of messages in order (the last is repeated if the run
    asks for more); ``bind_tools`` is a no-op so the agent can bind its tools."""

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        message = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _raising_tool(exc: Exception, name: str = "failing") -> StructuredTool:
    async def _run(**_: Any) -> str:
        raise exc

    return StructuredTool.from_function(func=None, coroutine=_run, name=name, description="a tool that raises")


def _tool_call(name: str, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": {}}])


def _status(result: str, status: str = "success") -> AIMessage:
    """A retrieval-agent terminal status envelope the routing node accepts."""
    return AIMessage(content=json.dumps({"status": status, "message": "m", "result": result}))


def _strip_callbacks(config: dict[str, Any]) -> dict[str, Any]:
    """A minimal ``init_langgraph_config`` stand-in: keep the thread_id and any
    recursion bound, drop the recording monitoring stub's non-callable callbacks the
    live graph would otherwise try to invoke."""
    out: dict[str, Any] = {"configurable": {"thread_id": config["configurable"]["thread_id"]}}
    if "recursion_limit" in config:
        out["recursion_limit"] = config["recursion_limit"]
    return out


def _thread(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


async def _collect(agen: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in agen]


# --------------------------------------------------------------------------
# langchain_deep_agent — real deepagents graph, driven through the run face
# --------------------------------------------------------------------------


class TestDeepAgentRecovery:
    def _seams(
        self, monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver, store: InMemoryStore
    ) -> None:
        from tai42_agents.langchain_deep_agent import agent as dagent

        monkeypatch.setattr(
            dagent,
            "llm_provider_settings",
            lambda: SimpleNamespace(
                llm="p", checkpoint="c", store="s", checkpoint_conn_string=None, store_conn_string=None
            ),
        )
        monkeypatch.setattr(dagent, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda k: dict(k)))

        async def get_llm(*, provider: str, **k: Any) -> Any:
            return model

        async def get_checkpointer(*, provider: str, conn_string: Any) -> Any:
            return saver

        async def get_store(*, provider: str, conn_string: Any, **k: Any) -> Any:
            return store

        monkeypatch.setattr(dagent, "get_llm_async", get_llm)
        monkeypatch.setattr(dagent, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=get_checkpointer))
        monkeypatch.setattr(dagent, "store_registry", lambda: SimpleNamespace(get_store=get_store))
        monkeypatch.setattr(dagent, "init_langgraph_config", _strip_callbacks)

    async def _graph(self, saver: InMemorySaver, store: InMemoryStore, model: BaseChatModel) -> Any:
        from tai42_agents.langchain_deep_agent.factory import build_langchain_deep_agent

        return await build_langchain_deep_agent(
            llm=model, store=store, checkpointer=saver, tools=[_raising_tool(RuntimeError("x"))]
        )

    def test_repair_heals_through_run_face(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver, store = InMemorySaver(), InMemoryStore()
        model = ScriptedChatModel([AIMessage(content="done")])

        async def seed() -> None:
            graph = await self._graph(saver, store, model)
            await graph.aupdate_state(
                _thread("t-deep-repair"),
                {"messages": [HumanMessage(content="first"), _tool_call("failing", "call_d")]},
                as_node=START,
            )

        asyncio.run(seed())
        self._seams(monkeypatch, model, saver, store)
        asyncio.run(
            tai42_app.agents.get_agent("langchain_deep_agent").run(
                tools=[], user_message="second", thread_id="t-deep-repair"
            )
        )

        async def read() -> list[BaseMessage]:
            graph = await self._graph(saver, store, model)
            snapshot = await graph.aget_state(_thread("t-deep-repair"))
            return list(snapshot.values.get("messages", []))

        repairs = [m for m in asyncio.run(read()) if isinstance(m, ToolMessage) and m.tool_call_id == "call_d"]
        assert len(repairs) == 1
        assert repairs[0].status == "error"
        assert repairs[0].content == rec._INTERRUPTED_TOOL_RESULT

    def test_tool_exception_visible_and_run_continues(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        saver, store = InMemorySaver(), InMemoryStore()
        model = ScriptedChatModel([_tool_call("failing", "call_1"), AIMessage(content="recovered")])
        self._seams(monkeypatch, model, saver, store)

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            out = asyncio.run(
                tai42_app.agents.get_agent("langchain_deep_agent").run(
                    tools=[_raising_tool(ToolException("tool said no"))], user_message="hi", thread_id="t-deep-tool"
                )
            )

        assert out == "recovered"
        assert any(
            "failing" in r.getMessage() and "call_1" in r.getMessage() and "tool said no" in r.getMessage()
            for r in caplog.records
        )

    def test_runtime_error_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver, store = InMemorySaver(), InMemoryStore()
        model = ScriptedChatModel([_tool_call("failing", "call_1"), AIMessage(content="unreached")])
        self._seams(monkeypatch, model, saver, store)

        with pytest.raises(RuntimeError, match="infra down"):
            asyncio.run(
                tai42_app.agents.get_agent("langchain_deep_agent").run(
                    tools=[_raising_tool(RuntimeError("infra down"))], user_message="hi", thread_id="t-deep-rt"
                )
            )

    def test_fresh_thread_untouched(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        saver, store = InMemorySaver(), InMemoryStore()
        model = ScriptedChatModel([AIMessage(content="hi there")])
        self._seams(monkeypatch, model, saver, store)

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            asyncio.run(
                tai42_app.agents.get_agent("langchain_deep_agent").run(
                    tools=[], user_message="hello", thread_id="t-deep-fresh"
                )
            )

        assert not any("repairing dangling tool_calls" in r.getMessage() for r in caplog.records)

    async def _build_with_worker(
        self, monkeypatch: pytest.MonkeyPatch, parent: BaseChatModel, worker: BaseChatModel, tool: StructuredTool
    ) -> Any:
        """A real deep agent whose ``worker`` subagent owns ``tool`` and runs on its
        own scripted model — the parent reaches it through the ``task`` tool."""
        from tai42_agents.langchain_deep_agent import factory as fac
        from tai42_agents.langchain_deep_agent.spec import ResolvedSubAgentSpec

        async def get_llm(*, provider: str, **k: Any) -> Any:
            return worker

        monkeypatch.setattr(fac, "get_llm_async", get_llm)
        spec = ResolvedSubAgentSpec(name="worker", description="w", system_prompt="sp", tools=[tool], llm_provider="w")
        return await fac.build_langchain_deep_agent(
            llm=parent, store=InMemoryStore(), checkpointer=InMemorySaver(), tools=[], subagents=[spec]
        )

    def _task_call(self) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"id": "t1", "name": "task", "args": {"description": "do it", "subagent_type": "worker"}}],
        )

    def test_nested_subagent_tool_exception_visible_and_continues(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        parent = ScriptedChatModel([self._task_call(), AIMessage(content="parent done")])
        worker = ScriptedChatModel([_tool_call("failing", "c1"), AIMessage(content="worker recovered")])
        agent = asyncio.run(
            self._build_with_worker(monkeypatch, parent, worker, _raising_tool(ToolException("tool said no")))
        )

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            res = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="hi")]}, _thread("t-sub-tool")))

        # The subagent surfaced the tool error to its model, finished, and the parent
        # received a useful result — the run did not abort.
        assert res["messages"][-1].content == "parent done"
        assert any(
            "failing" in r.getMessage() and "c1" in r.getMessage() and "tool said no" in r.getMessage()
            for r in caplog.records
        )

    def test_nested_subagent_runtime_error_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        parent = ScriptedChatModel([self._task_call(), AIMessage(content="unreached")])
        worker = ScriptedChatModel([_tool_call("failing", "c1"), AIMessage(content="unreached")])
        agent = asyncio.run(
            self._build_with_worker(monkeypatch, parent, worker, _raising_tool(RuntimeError("infra down")))
        )

        with pytest.raises(RuntimeError, match="infra down"):
            asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="hi")]}, _thread("t-sub-rt")))

    def test_grandchild_subagent_tool_exception_visible_and_continues(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Two levels deep: the leaf subagent (built via ``_compile_nested_subagent``)
        # owns the failing tool, so this pins the nested-build wiring specifically.
        from tai42_agents.langchain_deep_agent import factory as fac
        from tai42_agents.langchain_deep_agent.spec import ResolvedSubAgentSpec

        parent = ScriptedChatModel([self._task_call_to("mid"), AIMessage(content="parent done")])
        mid = ScriptedChatModel([self._task_call_to("leaf"), AIMessage(content="mid done")])
        leaf = ScriptedChatModel([_tool_call("failing", "c1"), AIMessage(content="leaf recovered")])
        models = {"mid": mid, "leaf": leaf}

        async def get_llm(*, provider: str, **k: Any) -> Any:
            return models[provider]

        monkeypatch.setattr(fac, "get_llm_async", get_llm)
        leaf_spec = ResolvedSubAgentSpec(
            name="leaf",
            description="l",
            system_prompt="sp",
            tools=[_raising_tool(ToolException("tool said no"))],
            llm_provider="leaf",
        )
        mid_spec = ResolvedSubAgentSpec(
            name="mid", description="m", system_prompt="sp", llm_provider="mid", subagents=[leaf_spec]
        )

        async def build() -> Any:
            return await fac.build_langchain_deep_agent(
                llm=parent, store=InMemoryStore(), checkpointer=InMemorySaver(), tools=[], subagents=[mid_spec]
            )

        agent = asyncio.run(build())
        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            res = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="hi")]}, _thread("t-gc-tool")))

        assert res["messages"][-1].content == "parent done"
        assert any(
            "failing" in r.getMessage() and "c1" in r.getMessage() and "tool said no" in r.getMessage()
            for r in caplog.records
        )

    def _task_call_to(self, subagent_type: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"id": "t1", "name": "task", "args": {"description": "go", "subagent_type": subagent_type}}],
        )

    def test_general_purpose_subagent_tool_exception_recovers_internally(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # deepagents auto-adds a general-purpose subagent inheriting the main tools; a
        # tool it calls raising ToolException must convert INSIDE the GP subagent — the
        # warning names the real failing tool (not the parent ``task``), the GP returns a
        # useful result, and the parent turn completes.
        from tai42_agents.langchain_deep_agent.factory import build_langchain_deep_agent

        model = ScriptedChatModel(
            [
                self._task_call_to("general-purpose"),
                _tool_call("failing", "c1"),
                AIMessage(content="gp recovered"),
                AIMessage(content="parent done"),
            ]
        )
        agent = asyncio.run(
            build_langchain_deep_agent(
                llm=model,
                store=InMemoryStore(),
                checkpointer=InMemorySaver(),
                tools=[_raising_tool(ToolException("tool said no"))],
            )
        )
        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            res = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="hi")]}, _thread("t-gp-tool")))

        assert res["messages"][-1].content == "parent done"
        assert any(
            "failing" in r.getMessage() and "c1" in r.getMessage() and "tool said no" in r.getMessage()
            for r in caplog.records
        )
        # The recovery is INSIDE the GP, not the parent catching a `task` abort.
        assert not any("'task'" in r.getMessage() for r in caplog.records)

    def test_general_purpose_subagent_runtime_error_aborts(self) -> None:
        from tai42_agents.langchain_deep_agent.factory import build_langchain_deep_agent

        model = ScriptedChatModel(
            [self._task_call_to("general-purpose"), _tool_call("failing", "c1"), AIMessage(content="unreached")]
        )
        agent = asyncio.run(
            build_langchain_deep_agent(
                llm=model,
                store=InMemoryStore(),
                checkpointer=InMemorySaver(),
                tools=[_raising_tool(RuntimeError("infra down"))],
            )
        )
        with pytest.raises(RuntimeError, match="infra down"):
            asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="hi")]}, _thread("t-gp-rt")))

    def test_nested_subagent_carries_general_purpose_with_shared_middleware(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # At every nesting depth the leaf's own auto-added general-purpose subagent
        # carries the shared middleware — pinned by introspecting the nested build.
        from tai42_agents.langchain_deep_agent import factory as fac
        from tai42_agents.langchain_deep_agent.spec import ResolvedSubAgentSpec

        captured: dict[str, Any] = {}

        def fake_create(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(fac, "create_deep_agent", fake_create)
        child = ResolvedSubAgentSpec(
            name="leaf", description="l", system_prompt="sp", tools=[_raising_tool(ToolException("x"))]
        )
        asyncio.run(
            fac._compile_nested_subagent(
                child, parent_model="LLM", parent_tools=[], store=InMemoryStore(), backend=object()
            )
        )
        gp = [s for s in captured["subagents"] if s.get("name") == "general-purpose"]
        assert len(gp) == 1
        assert rec._tool_error_middleware in gp[0]["middleware"]


# --------------------------------------------------------------------------
# refine_agent — real create_agent evaluator/critic, driven through run
# --------------------------------------------------------------------------


class TestRefineAgentRecovery:
    def _seams(self, monkeypatch: pytest.MonkeyPatch, models: dict[str, BaseChatModel], saver: InMemorySaver) -> None:
        from tai42_agents.refine_agent import agent as fagent

        monkeypatch.setattr(
            fagent,
            "llm_provider_settings",
            lambda: SimpleNamespace(llm="eval", checkpoint="c", checkpoint_conn_string=None),
        )
        monkeypatch.setattr(fagent, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda k: dict(k)))
        monkeypatch.setattr(fagent, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: False))
        monkeypatch.setattr(fagent, "context_overflow_middlewares", lambda system_prompt=None: [])

        async def get_llm(*, provider: str, **k: Any) -> Any:
            return models[provider]

        async def get_checkpointer(*, provider: str, conn_string: Any) -> Any:
            return saver

        monkeypatch.setattr(fagent, "get_llm_async", get_llm)
        monkeypatch.setattr(fagent, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=get_checkpointer))
        monkeypatch.setattr(fagent, "init_langgraph_config", _strip_callbacks)

    def _approval(self) -> str:
        from tai42_agents.refine_agent.prompt import CRITIC_APPROVAL_MESSAGE

        return CRITIC_APPROVAL_MESSAGE

    def _run(self, **kwargs: Any) -> Any:
        return asyncio.run(
            tai42_app.agents.get_agent("refine_agent").run(
                evaluator_message="draft it",
                critic_message="review it",
                evaluator_llm_provider="eval",
                critic_llm_provider="crit",
                evaluator_langgraph_config=_thread("eval-t"),
                critic_langgraph_config=_thread("crit-t"),
                max_iterations=2,
                **kwargs,
            )
        )

    def test_repair_heals_evaluator_thread_through_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        eval_model = ScriptedChatModel([AIMessage(content="draft"), AIMessage(content="final answer")])
        crit_model = ScriptedChatModel([AIMessage(content=f"approved {self._approval()}")])

        seed = create_agent(eval_model, tools=[], checkpointer=saver)
        asyncio.run(
            seed.aupdate_state(
                _thread("eval-t"),
                {"messages": [HumanMessage(content="first"), _tool_call("failing", "call_r")]},
                as_node=START,
            )
        )

        self._seams(monkeypatch, {"eval": eval_model, "crit": crit_model}, saver)
        assert self._run() == "final answer"

        reader = create_agent(eval_model, tools=[], checkpointer=saver)
        messages = asyncio.run(reader.aget_state(_thread("eval-t"))).values["messages"]
        repairs = [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "call_r"]
        assert len(repairs) == 1
        assert repairs[0].content == rec._INTERRUPTED_TOOL_RESULT

    def test_tool_exception_visible_and_loop_continues(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, app_tools: Any
    ) -> None:
        saver = InMemorySaver()
        app_tools.client_tools["failing"] = _raising_tool(ToolException("tool said no"))
        # Evaluator calls the failing tool, sees the error ToolMessage, then drafts.
        eval_model = ScriptedChatModel(
            [_tool_call("failing", "call_1"), AIMessage(content="draft"), AIMessage(content="final answer")]
        )
        crit_model = ScriptedChatModel([AIMessage(content=f"approved {self._approval()}")])
        self._seams(monkeypatch, {"eval": eval_model, "crit": crit_model}, saver)

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            out = self._run(tool_names=["failing"])

        assert out == "final answer"
        assert any(
            "failing" in r.getMessage() and "call_1" in r.getMessage() and "tool said no" in r.getMessage()
            for r in caplog.records
        )

    def test_runtime_error_aborts(self, monkeypatch: pytest.MonkeyPatch, app_tools: Any) -> None:
        saver = InMemorySaver()
        app_tools.client_tools["failing"] = _raising_tool(RuntimeError("infra down"))
        eval_model = ScriptedChatModel([_tool_call("failing", "call_1"), AIMessage(content="unreached")])
        crit_model = ScriptedChatModel([AIMessage(content=f"approved {self._approval()}")])
        self._seams(monkeypatch, {"eval": eval_model, "crit": crit_model}, saver)

        with pytest.raises(RuntimeError, match="infra down"):
            self._run(tool_names=["failing"])


# --------------------------------------------------------------------------
# retrieval_tools_agent — real graph; repair through the run face, and the
# main tool-execution node's error semantics through the projection
# --------------------------------------------------------------------------


class TestRetrievalAgentRecovery:
    def _seams(self, monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
        from tai42_agents.retrieval_tools_agent import agent as ragent

        monkeypatch.setattr(
            ragent,
            "llm_provider_settings",
            lambda: SimpleNamespace(
                llm="p", embedding="e", checkpoint="c", store="s", checkpoint_conn_string=None, store_conn_string=None
            ),
        )
        monkeypatch.setattr(ragent, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda k: dict(k)))
        monkeypatch.setattr(ragent, "embedding_settings", lambda: SimpleNamespace(with_fallbacks=lambda k: dict(k)))

        async def get_llm(*, provider: str, **k: Any) -> Any:
            return model

        embedding = SimpleNamespace(aembed_query=lambda text: _aembed())

        async def get_embedding(*, provider: str, **k: Any) -> Any:
            return embedding

        async def get_store(*, provider: str, conn_string: Any, **k: Any) -> Any:
            return InMemoryStore()

        async def get_checkpointer(*, provider: str, conn_string: Any) -> Any:
            return saver

        monkeypatch.setattr(ragent, "get_llm_async", get_llm)
        monkeypatch.setattr(ragent, "get_embedding_async", get_embedding)
        monkeypatch.setattr(ragent, "store_registry", lambda: SimpleNamespace(get_store=get_store))
        monkeypatch.setattr(ragent, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=get_checkpointer))
        monkeypatch.setattr(ragent, "init_langgraph_config", _strip_callbacks)

    def test_repair_heals_through_run_face(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph

        saver = InMemorySaver()
        model = ScriptedChatModel([_status("done")])

        async def seed() -> None:
            graph = await RetrievalToolsGraph(tools=[], llm=model, store=InMemoryStore(), checkpoint=saver).abuild()
            await graph.aupdate_state(
                _thread("t-retr-repair"),
                {"messages": [HumanMessage(content="first"), _tool_call("failing", "call_r")]},
                as_node=START,
            )

        asyncio.run(seed())
        self._seams(monkeypatch, model, saver)
        out = asyncio.run(
            tai42_app.agents.get_agent("retrieval_tools_agent").run(user_message="second", thread_id="t-retr-repair")
        )
        assert out == "done"

        async def read() -> list[BaseMessage]:
            graph = await RetrievalToolsGraph(tools=[], llm=model, store=InMemoryStore(), checkpoint=saver).abuild()
            return list((await graph.aget_state(_thread("t-retr-repair"))).values.get("messages", []))

        repairs = [m for m in asyncio.run(read()) if isinstance(m, ToolMessage) and m.tool_call_id == "call_r"]
        assert len(repairs) == 1
        assert repairs[0].content == rec._INTERRUPTED_TOOL_RESULT

    def test_main_tool_exception_projects_error_result(self, caplog: pytest.LogCaptureFixture) -> None:
        from tai42_kit.utils.data.string_util import text_to_md5

        from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph

        class _StubStore(InMemoryStore):
            def __init__(self, keys: list[str]) -> None:
                super().__init__()
                self._keys = keys

            async def asearch(self, namespace_prefix: Any, /, **kwargs: Any) -> Any:
                return [SimpleNamespace(key=key) for key in self._keys]

        alpha = _raising_tool(ToolException("no good"), name="alpha")
        alpha_id = text_to_md5("alpha")
        model = ScriptedChatModel(
            [
                AIMessage(content="", tool_calls=[{"id": "r1", "name": "retrieve_tools", "args": {"query": "a"}}]),
                _tool_call("alpha", "a1"),
                _status("", status="error"),
            ]
        )
        graph = asyncio.run(RetrievalToolsGraph(tools=[alpha], llm=model, store=_StubStore([alpha_id])).abuild())

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            events = asyncio.run(
                _collect(
                    aproject_agent_events(
                        graph,
                        {"messages": [{"role": "user", "content": "x"}]},
                        {"configurable": {"thread_id": "t-retr-tool"}},
                    )
                )
            )
        alpha_results = [e for e in events if isinstance(e, ToolResultStep) and e.tool == "alpha"]
        assert len(alpha_results) == 1
        assert alpha_results[0].is_error is True
        assert "no good" in str(alpha_results[0].result)
        # The raw ToolNode path reaches the shared middleware, which logs the failure.
        assert any(
            "alpha" in r.getMessage() and "a1" in r.getMessage() and "no good" in r.getMessage() for r in caplog.records
        )

    def test_main_tool_runtime_error_aborts(self) -> None:
        from tai42_kit.utils.data.string_util import text_to_md5

        from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph

        class _StubStore(InMemoryStore):
            def __init__(self, keys: list[str]) -> None:
                super().__init__()
                self._keys = keys

            async def asearch(self, namespace_prefix: Any, /, **kwargs: Any) -> Any:
                return [SimpleNamespace(key=key) for key in self._keys]

        alpha = _raising_tool(RuntimeError("infra down"), name="alpha")
        alpha_id = text_to_md5("alpha")
        model = ScriptedChatModel(
            [
                AIMessage(content="", tool_calls=[{"id": "r1", "name": "retrieve_tools", "args": {"query": "a"}}]),
                _tool_call("alpha", "a1"),
                _status(""),
            ]
        )
        graph = asyncio.run(RetrievalToolsGraph(tools=[alpha], llm=model, store=_StubStore([alpha_id])).abuild())

        with pytest.raises(RuntimeError, match="infra down"):
            asyncio.run(
                _collect(
                    aproject_agent_events(
                        graph,
                        {"messages": [{"role": "user", "content": "x"}]},
                        {"configurable": {"thread_id": "t-retr-rt"}},
                    )
                )
            )


async def _aembed() -> list[float]:
    return [0.0, 0.1, 0.2]
