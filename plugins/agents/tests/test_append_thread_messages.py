"""``append_thread_messages`` across the graph-backed agents that address one thread.

Each implementing agent (``tools_agent``, ``deep_agent``, ``retrieval_tools_agent``)
writes the role/content messages straight into its thread's checkpoint through the
graph's ``START`` node — no model call — so a later run on the same thread reads
them as prior history. The tests drive each agent's real compile path with a
scripted fake chat model and an in-memory checkpointer (no LLM, no network),
patching the same provider seams the recovery tests do. The agents that cannot
address a single thread (``refine_agent`` / ``voting_agent`` — two role graphs) and
the one with no thread at all (``vqa_agent``) implement nothing and inherit the
contract's ``NotImplementedError``.

Async code is driven with ``asyncio.run`` (the suite does not use pytest-asyncio).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import START
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr
from tai42_contract.app import tai42_app
from tai42_kit.llm.middleware.leading_user import _CONVERSATION_START_MARKER

# Importing each agent class registers its module through the recording app
# decorator, the same way the host binds the app and imports the modules its
# manifest names; the classes drive the structural registration checks below.
from tai42_agents._internal import append as append_mod
from tai42_agents._internal import recovery as rec
from tai42_agents.deep_agent.agent import DeepAgent
from tai42_agents.mcp_tools_agent import McpToolsAgent
from tai42_agents.refine_agent.agent import RefineAgent
from tai42_agents.retrieval_tools_agent.agent import RetrievalToolsAgent
from tai42_agents.tools_agent import ToolsAgent
from tai42_agents.voting_agent.agent import VotingAgent
from tai42_agents.vqa_agent import VqaAgent

_THREAD_AGENTS = [
    ("tools_agent", ToolsAgent),
    ("deep_agent", DeepAgent),
    ("retrieval_tools_agent", RetrievalToolsAgent),
    ("mcp_tools_agent", McpToolsAgent),
]
_NON_THREAD_AGENTS = [
    ("vqa_agent", VqaAgent),
    ("voting_agent", VotingAgent),
    ("refine_agent", RefineAgent),
]


class ScriptedChatModel(BaseChatModel):
    """Returns a fixed list of messages in order (the last repeated if asked again)
    and records every message list it is asked to generate from; ``bind_tools`` is a
    no-op so an agent can bind its tools."""

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def seen(self) -> list[BaseMessage]:
        return [message for call in self._seen for message in call]

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        message = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _strip_callbacks(config: dict[str, Any]) -> dict[str, Any]:
    """A minimal ``init_langgraph_config`` stand-in: keep the thread_id and any
    recursion bound, drop the recording monitoring stub's non-callable callbacks a
    live graph would otherwise try to invoke."""
    out: dict[str, Any] = {"configurable": {"thread_id": config["configurable"]["thread_id"]}}
    if "recursion_limit" in config:
        out["recursion_limit"] = config["recursion_limit"]
    return out


def _thread(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _tool_call(name: str, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": {}}])


_PRIOR = [
    {"role": "user", "content": "prior turn"},
    {"role": "assistant", "content": "prior reply"},
]


def _assert_role_mapping(messages: list[BaseMessage]) -> None:
    """The appended ``user`` / ``assistant`` messages land as ``HumanMessage`` /
    ``AIMessage`` carrying their content, in order."""
    human = [m for m in messages if isinstance(m, HumanMessage) and m.content == "prior turn"]
    ai = [m for m in messages if isinstance(m, AIMessage) and m.content == "prior reply"]
    assert len(human) == 1
    assert len(ai) == 1


def _assert_run_saw_prior(model: ScriptedChatModel) -> None:
    contents = [str(getattr(m, "content", "")) for m in model.seen]
    assert any("prior turn" in c for c in contents)
    assert any("prior reply" in c for c in contents)


# An operator message can open a thread before any client turn: its only
# checkpointed message is an assistant message.
_OPERATOR_OPENER = [{"role": "assistant", "content": "operator opener"}]


def _assert_leads_user_first(model: ScriptedChatModel) -> None:
    """The first history the model was asked to generate from leads user-first: the
    leading-user marker sits ahead of the operator's assistant message, so a
    strict-ordering provider would accept the turn."""
    non_system = [m for m in model._seen[0] if not isinstance(m, SystemMessage)]
    assert isinstance(non_system[0], HumanMessage)
    assert non_system[0].content == _CONVERSATION_START_MARKER
    operator = next((i for i, m in enumerate(non_system) if isinstance(m, AIMessage)), None)
    assert operator is not None
    assert non_system[operator].content == "operator opener"
    assert operator > 0


# --------------------------------------------------------------------------
# tools_agent — the shared base-tool compile path
# --------------------------------------------------------------------------


class TestToolsAgentAppend:
    def _seams(self, monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
        from tai42_agents._internal import base_tool_agent as bta

        monkeypatch.setattr(
            bta,
            "llm_provider_settings",
            lambda: SimpleNamespace(llm="p", checkpoint="c", checkpoint_conn_string=None),
        )
        monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda k: dict(k)))
        monkeypatch.setattr(bta, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: False))
        monkeypatch.setattr(bta, "context_overflow_middlewares", lambda system_prompt=None: [])
        monkeypatch.setattr(bta, "init_langgraph_config", _strip_callbacks)

        async def get_llm(*, provider: str, **k: Any) -> Any:
            return model

        async def get_checkpointer(*, provider: str, conn_string: Any) -> Any:
            return saver

        monkeypatch.setattr(bta, "get_llm_async", get_llm)
        monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=get_checkpointer))

    def _read(self, saver: InMemorySaver, model: BaseChatModel, thread_id: str) -> list[BaseMessage]:
        graph = create_agent(model, tools=[], checkpointer=saver)
        snapshot = asyncio.run(graph.aget_state(_thread(thread_id)))
        return list(snapshot.values.get("messages", []))

    def test_role_mapping_persists_to_checkpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._seams(monkeypatch, model, saver)
        agent = tai42_app.agents.get_agent("tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-tools-map", messages=_PRIOR))
        _assert_role_mapping(self._read(saver, model, "t-tools-map"))

    def test_run_sees_appended_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._seams(monkeypatch, model, saver)
        agent = tai42_app.agents.get_agent("tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-tools-run", messages=_PRIOR))
        out = asyncio.run(agent.run(user_message="new turn", thread_id="t-tools-run"))
        assert out == "reply"
        _assert_run_saw_prior(model)

    def test_operator_opened_thread_leads_user_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._seams(monkeypatch, model, saver)
        agent = tai42_app.agents.get_agent("tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-tools-op", messages=_OPERATOR_OPENER))
        asyncio.run(agent.run(user_message="client turn", thread_id="t-tools-op"))
        _assert_leads_user_first(model)

    def test_append_heals_dangling_tool_call_before_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._seams(monkeypatch, model, saver)

        # An aborted run left the thread's last message an AIMessage with an
        # unanswered tool_call; appending would push it into the history's interior
        # where the turn-start repair can never reach it again.
        seed = create_agent(model, tools=[], checkpointer=saver)
        asyncio.run(
            seed.aupdate_state(
                _thread("t-tools-heal"),
                {"messages": [HumanMessage(content="first"), _tool_call("failing", "call_x")]},
                as_node=START,
            )
        )

        agent = tai42_app.agents.get_agent("tools_agent")
        asyncio.run(
            agent.append_thread_messages(thread_id="t-tools-heal", messages=[{"role": "user", "content": "second"}])
        )

        # The synthetic error ToolMessage answering call_x sits between the dangling
        # AIMessage and the appended HumanMessage: the interior tool_call is answered.
        persisted = self._read(saver, model, "t-tools-heal")
        repairs = [m for m in persisted if isinstance(m, ToolMessage) and m.tool_call_id == "call_x"]
        assert len(repairs) == 1
        assert repairs[0].content == rec._INTERRUPTED_TOOL_RESULT
        ai_idx = next(i for i, m in enumerate(persisted) if isinstance(m, AIMessage) and m.tool_calls)
        repair_idx = persisted.index(repairs[0])
        human_idx = next(i for i, m in enumerate(persisted) if isinstance(m, HumanMessage) and m.content == "second")
        assert ai_idx < repair_idx < human_idx

        # A real turn now proceeds: the history handed to the model has no unanswered
        # tool_call, so a provider would not reject it.
        out = asyncio.run(agent.run(user_message="third", thread_id="t-tools-heal"))
        assert out == "reply"
        seen_repairs = [m for m in model.seen if isinstance(m, ToolMessage) and m.tool_call_id == "call_x"]
        assert len(seen_repairs) == 1
        assert seen_repairs[0].content == rec._INTERRUPTED_TOOL_RESULT

    def test_append_no_op_on_healthy_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._seams(monkeypatch, model, saver)

        seed = create_agent(model, tools=[], checkpointer=saver)
        asyncio.run(
            seed.aupdate_state(
                _thread("t-tools-healthy"),
                {"messages": [HumanMessage(content="first"), AIMessage(content="a plain reply")]},
                as_node=START,
            )
        )

        agent = tai42_app.agents.get_agent("tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-tools-healthy", messages=_PRIOR))

        # A healthy last message carries no unanswered tool_call, so the repair inserts
        # nothing: no ToolMessage, no synthetic interrupted-tool marker.
        persisted = self._read(saver, model, "t-tools-healthy")
        assert not [m for m in persisted if isinstance(m, ToolMessage)]
        assert not [m for m in persisted if getattr(m, "content", None) == rec._INTERRUPTED_TOOL_RESULT]

    def test_invalid_role_raises(self) -> None:
        agent = tai42_app.agents.get_agent("tools_agent")
        with pytest.raises(ValueError, match="role 'system'"):
            asyncio.run(agent.append_thread_messages(thread_id="t", messages=[{"role": "system", "content": "x"}]))

    def test_blank_content_raises(self) -> None:
        agent = tai42_app.agents.get_agent("tools_agent")
        with pytest.raises(ValueError, match="blank content"):
            asyncio.run(agent.append_thread_messages(thread_id="t", messages=[{"role": "user", "content": "  "}]))

    def test_missing_thread_id_raises(self) -> None:
        agent = tai42_app.agents.get_agent("tools_agent")
        with pytest.raises(ValueError, match="requires a thread_id"):
            asyncio.run(agent.append_thread_messages(thread_id=None, messages=_PRIOR))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# deep_agent — the real deepagents graph
# --------------------------------------------------------------------------


class TestDeepAgentAppend:
    def _seams(
        self, monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver, store: InMemoryStore
    ) -> None:
        from tai42_agents.deep_agent import agent as dagent

        monkeypatch.setattr(
            dagent,
            "llm_provider_settings",
            lambda: SimpleNamespace(
                llm="p", checkpoint="c", store="s", checkpoint_conn_string=None, store_conn_string=None
            ),
        )
        monkeypatch.setattr(dagent, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda k: dict(k)))
        monkeypatch.setattr(dagent, "init_langgraph_config", _strip_callbacks)

        async def get_llm(*, provider: str, **k: Any) -> Any:
            return model

        async def get_checkpointer(*, provider: str, conn_string: Any) -> Any:
            return saver

        async def get_store(*, provider: str, conn_string: Any, **k: Any) -> Any:
            return store

        monkeypatch.setattr(dagent, "get_llm_async", get_llm)
        monkeypatch.setattr(dagent, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=get_checkpointer))
        monkeypatch.setattr(dagent, "store_registry", lambda: SimpleNamespace(get_store=get_store))

    def _read(
        self, saver: InMemorySaver, store: InMemoryStore, model: BaseChatModel, thread_id: str
    ) -> list[BaseMessage]:
        from tai42_agents.deep_agent.factory import build_deep_agent

        graph = asyncio.run(build_deep_agent(llm=model, store=store, checkpointer=saver, tools=[]))
        snapshot = asyncio.run(graph.aget_state(_thread(thread_id)))
        return list(snapshot.values.get("messages", []))

    def test_role_mapping_persists_to_checkpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver, store = InMemorySaver(), InMemoryStore()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._seams(monkeypatch, model, saver, store)
        agent = tai42_app.agents.get_agent("deep_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-deep-map", messages=_PRIOR))
        _assert_role_mapping(self._read(saver, store, model, "t-deep-map"))

    def test_run_sees_appended_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver, store = InMemorySaver(), InMemoryStore()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._seams(monkeypatch, model, saver, store)
        agent = tai42_app.agents.get_agent("deep_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-deep-run", messages=_PRIOR))
        out = asyncio.run(agent.run(tools=[], user_message="new turn", thread_id="t-deep-run"))
        assert out == "reply"
        _assert_run_saw_prior(model)

    def test_operator_opened_thread_leads_user_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver, store = InMemorySaver(), InMemoryStore()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._seams(monkeypatch, model, saver, store)
        agent = tai42_app.agents.get_agent("deep_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-deep-op", messages=_OPERATOR_OPENER))
        asyncio.run(agent.run(tools=[], user_message="client turn", thread_id="t-deep-op"))
        _assert_leads_user_first(model)

    def test_invalid_role_raises(self) -> None:
        agent = tai42_app.agents.get_agent("deep_agent")
        with pytest.raises(ValueError, match="role 'tool'"):
            asyncio.run(agent.append_thread_messages(thread_id="t", messages=[{"role": "tool", "content": "x"}]))

    def test_blank_content_raises(self) -> None:
        agent = tai42_app.agents.get_agent("deep_agent")
        with pytest.raises(ValueError, match="blank content"):
            asyncio.run(agent.append_thread_messages(thread_id="t", messages=[{"role": "assistant", "content": ""}]))

    def test_missing_thread_id_raises(self) -> None:
        agent = tai42_app.agents.get_agent("deep_agent")
        with pytest.raises(ValueError, match="requires a thread_id"):
            asyncio.run(agent.append_thread_messages(thread_id=None, messages=_PRIOR))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# retrieval_tools_agent — the real retrieval graph
# --------------------------------------------------------------------------


def _status(result: str) -> AIMessage:
    import json

    return AIMessage(content=json.dumps({"status": "success", "message": "m", "result": result}))


class TestRetrievalAgentAppend:
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
        monkeypatch.setattr(ragent, "init_langgraph_config", _strip_callbacks)

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

    def _read(self, saver: InMemorySaver, model: BaseChatModel, thread_id: str) -> list[BaseMessage]:
        from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph

        graph = asyncio.run(RetrievalToolsGraph(tools=[], llm=model, store=InMemoryStore(), checkpoint=saver).abuild())
        snapshot = asyncio.run(graph.aget_state(_thread(thread_id)))
        return list(snapshot.values.get("messages", []))

    def test_role_mapping_persists_to_checkpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([_status("done")])
        self._seams(monkeypatch, model, saver)
        agent = tai42_app.agents.get_agent("retrieval_tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-retr-map", messages=_PRIOR))
        _assert_role_mapping(self._read(saver, model, "t-retr-map"))

    def test_run_sees_appended_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([_status("done")])
        self._seams(monkeypatch, model, saver)
        agent = tai42_app.agents.get_agent("retrieval_tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-retr-run", messages=_PRIOR))
        out = asyncio.run(agent.run(user_message="new turn", thread_id="t-retr-run"))
        assert out == "done"
        _assert_run_saw_prior(model)

    def test_operator_opened_thread_leads_user_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([_status("done")])
        self._seams(monkeypatch, model, saver)
        agent = tai42_app.agents.get_agent("retrieval_tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-retr-op", messages=_OPERATOR_OPENER))
        asyncio.run(agent.run(user_message="client turn", thread_id="t-retr-op"))
        _assert_leads_user_first(model)

    def test_invalid_role_raises(self) -> None:
        agent = tai42_app.agents.get_agent("retrieval_tools_agent")
        with pytest.raises(ValueError, match="role 'assistant '"):
            asyncio.run(agent.append_thread_messages(thread_id="t", messages=[{"role": "assistant ", "content": "x"}]))

    def test_blank_content_raises(self) -> None:
        agent = tai42_app.agents.get_agent("retrieval_tools_agent")
        with pytest.raises(ValueError, match="blank content"):
            asyncio.run(agent.append_thread_messages(thread_id="t", messages=[{"role": "user", "content": ""}]))

    def test_missing_thread_id_raises(self) -> None:
        agent = tai42_app.agents.get_agent("retrieval_tools_agent")
        with pytest.raises(ValueError, match="requires a thread_id"):
            asyncio.run(agent.append_thread_messages(thread_id=None, messages=_PRIOR))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# mcp_tools_agent — the shared base-tool compile path; a run reads the append
# with the MCP client stubbed (the checkpoint write never opens a server)
# --------------------------------------------------------------------------


class _FakeMcpClient:
    """A no-op async context manager standing in for ``fastmcp.Client`` so a run's
    tool-loading never opens a real server."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    async def __aenter__(self) -> _FakeMcpClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class TestMcpToolsAgentAppend:
    def _base_seams(self, monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
        from tai42_agents._internal import base_tool_agent as bta

        monkeypatch.setattr(
            bta,
            "llm_provider_settings",
            lambda: SimpleNamespace(llm="p", checkpoint="c", checkpoint_conn_string=None),
        )
        monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda k: dict(k)))
        monkeypatch.setattr(bta, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: False))
        monkeypatch.setattr(bta, "context_overflow_middlewares", lambda system_prompt=None: [])
        monkeypatch.setattr(bta, "init_langgraph_config", _strip_callbacks)

        async def get_llm(*, provider: str, **k: Any) -> Any:
            return model

        async def get_checkpointer(*, provider: str, conn_string: Any) -> Any:
            return saver

        monkeypatch.setattr(bta, "get_llm_async", get_llm)
        monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=get_checkpointer))

    def _run_seams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tai42_agents import mcp_tools_agent as mcp_mod

        async def fake_tools(client: Any) -> list[Any]:
            return []

        monkeypatch.setattr(mcp_mod, "Client", _FakeMcpClient)
        monkeypatch.setattr(mcp_mod, "mcp_tools_to_lc_tools", fake_tools)

    def _read(self, saver: InMemorySaver, model: BaseChatModel, thread_id: str) -> list[BaseMessage]:
        graph = create_agent(model, tools=[], checkpointer=saver)
        snapshot = asyncio.run(graph.aget_state(_thread(thread_id)))
        return list(snapshot.values.get("messages", []))

    def test_role_mapping_persists_to_checkpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._base_seams(monkeypatch, model, saver)
        agent = tai42_app.agents.get_agent("mcp_tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-mcp-map", messages=_PRIOR))
        _assert_role_mapping(self._read(saver, model, "t-mcp-map"))

    def test_run_sees_appended_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._base_seams(monkeypatch, model, saver)
        self._run_seams(monkeypatch)
        agent = tai42_app.agents.get_agent("mcp_tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-mcp-run", messages=_PRIOR))
        out = asyncio.run(agent.run(mcp_config={"mcpServers": {}}, user_message="new turn", thread_id="t-mcp-run"))
        assert out == "reply"
        _assert_run_saw_prior(model)

    def test_operator_opened_thread_leads_user_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="reply")])
        self._base_seams(monkeypatch, model, saver)
        self._run_seams(monkeypatch)
        agent = tai42_app.agents.get_agent("mcp_tools_agent")
        asyncio.run(agent.append_thread_messages(thread_id="t-mcp-op", messages=_OPERATOR_OPENER))
        asyncio.run(agent.run(mcp_config={"mcpServers": {}}, user_message="client turn", thread_id="t-mcp-op"))
        _assert_leads_user_first(model)

    def test_invalid_role_raises(self) -> None:
        agent = tai42_app.agents.get_agent("mcp_tools_agent")
        with pytest.raises(ValueError, match="role 'system'"):
            asyncio.run(agent.append_thread_messages(thread_id="t", messages=[{"role": "system", "content": "x"}]))

    def test_blank_content_raises(self) -> None:
        agent = tai42_app.agents.get_agent("mcp_tools_agent")
        with pytest.raises(ValueError, match="blank content"):
            asyncio.run(agent.append_thread_messages(thread_id="t", messages=[{"role": "user", "content": "  "}]))

    def test_missing_thread_id_raises(self) -> None:
        agent = tai42_app.agents.get_agent("mcp_tools_agent")
        with pytest.raises(ValueError, match="requires a thread_id"):
            asyncio.run(agent.append_thread_messages(thread_id=None, messages=_PRIOR))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The generic conversion helper
# --------------------------------------------------------------------------


def test_to_thread_messages_maps_roles() -> None:
    converted = append_mod.to_thread_messages(_PRIOR)
    assert isinstance(converted[0], HumanMessage)
    assert converted[0].content == "prior turn"
    assert isinstance(converted[1], AIMessage)
    assert converted[1].content == "prior reply"


def test_to_thread_messages_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        append_mod.to_thread_messages(["not-a-dict"])  # type: ignore[list-item]


# --------------------------------------------------------------------------
# Agents that cannot address a single thread inherit the contract default
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "cls"), _NON_THREAD_AGENTS)
def test_non_thread_agents_do_not_implement_append(name: str, cls: type) -> None:
    """``vqa_agent`` (no checkpointer), ``voting_agent`` and ``refine_agent`` (two
    role graphs, no single thread) define no ``append_thread_messages`` of their own
    and inherit the contract's ``NotImplementedError``."""
    agent = tai42_app.agents.get_agent(name)
    assert isinstance(agent, cls)
    assert "append_thread_messages" not in cls.__dict__
    with pytest.raises(NotImplementedError):
        asyncio.run(agent.append_thread_messages(thread_id="t", messages=[{"role": "user", "content": "x"}]))


@pytest.mark.parametrize(("name", "cls"), _THREAD_AGENTS)
def test_thread_agents_implement_append(name: str, cls: type) -> None:
    """The single-thread graph-backed agents each define their own
    ``append_thread_messages`` rather than inheriting the default."""
    agent = tai42_app.agents.get_agent(name)
    assert isinstance(agent, cls)
    assert "append_thread_messages" in cls.__dict__


async def _aembed() -> list[float]:
    return [0.0, 0.1, 0.2]
