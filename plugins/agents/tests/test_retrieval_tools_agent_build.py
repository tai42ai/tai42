"""``retrieval_tools_agent`` build, the ``astream`` and ``run`` faces with the
reject-unhonored guards, and the input model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.agent import (
    MessageFinal,
    StructuredFinal,
)
from tai42_contract.template import TemplatedText

from tai42_agents.retrieval_tools_agent import agent as ragent
from tai42_agents.retrieval_tools_agent.agent import (
    RetrievalToolsAgent,
    RetrievalToolsAgentInput,
)

from ._retrieval_tools_agent_support import (
    _RETRIEVAL_SCHEMA,
    _collect,
    _NativeFake,
    _patch_build_seams,
    _ToolFake,
)


class TestBuild:
    def test_resolves_defaults_and_returns_agent_messages_config(
        self, monkeypatch: pytest.MonkeyPatch, resource_manager: Any
    ) -> None:
        captured = _patch_build_seams(monkeypatch)
        agent = RetrievalToolsAgent()

        compiled, messages, config, llm, minted = asyncio.run(
            agent._build(
                system_message=TemplatedText(content="be brief"),
                user_message=TemplatedText(content="do it"),
                tools_limit=7,
            )
        )

        assert compiled is captured["compiled"]
        assert config == {"configurable": {"thread_id": "t"}}
        assert minted == "t"
        # ``_build`` hands back the resolved llm for the structured finalization pass.
        assert llm == "llm-obj"
        # The rendered user message is the whole agent input; the system prompt is
        # per-run graph configuration (prepended at each model call, never
        # checkpointed state) and reaches the graph constructor instead.
        assert messages == {"messages": [{"role": "user", "content": "do it"}]}
        assert "be brief" in captured["graph_kwargs"]["system_prompt"]

        # Providers fell back to the settings defaults and were threaded through.
        assert captured["llm_provider"] == "mistral"
        assert captured["embedding_provider"] == "def_embedding"
        assert captured["store"][0] == "def_store"
        assert captured["store"][1] == "store-conn"
        assert captured["checkpoint"] == ("def_checkpoint", "checkpoint-conn")
        # Embedding dims probed (6) and passed into the store index spec.
        assert captured["store"][2]["index"]["dims"] == 6
        # Resolved tools + limit reach the graph.
        assert captured["graph_kwargs"]["tools"] == ["resolved-tool"]
        assert captured["graph_kwargs"]["tools_limit"] == 7

    def test_explicit_providers_override_defaults(self, monkeypatch: pytest.MonkeyPatch, resource_manager: Any) -> None:
        captured = _patch_build_seams(monkeypatch)
        agent = RetrievalToolsAgent()

        asyncio.run(
            agent._build(
                user_message=TemplatedText(content="hi"),
                llm_provider="xai",
                embedding_provider="my_embedding",
                checkpoint_provider="my_checkpoint",
                store_provider="my_store",
            )
        )

        assert captured["llm_provider"] == "xai"
        assert captured["embedding_provider"] == "my_embedding"
        assert captured["store"][0] == "my_store"
        assert captured["checkpoint"][0] == "my_checkpoint"

    def test_run_raises_when_required_user_message_slot_is_unset(self, resource_manager: Any) -> None:
        """``_build`` renders the user message with ``allow_empty=False``: an unset
        slot (``None``) raises loudly rather than silently building on an empty prompt.
        Reached through the real ``run`` face — before any provider seam — so a dropped
        render.py ``allow_empty`` passthrough turns this red."""
        agent = RetrievalToolsAgent()
        with pytest.raises(ValueError, match="required message was not provided"):
            asyncio.run(agent.run())


# Keywords this agent does not declare: each is refused loudly on both faces, never dropped.
_UNDECLARED_CASES = [
    ("subagents", [object()]),
    ("strategy", ""),
    ("skills", ["s"]),
    ("inline_skills", [{"name": "n", "content": "c"}]),
    ("interrupt_on", {}),
    ("resume", False),
    ("system_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
]


class TestAstreamAndRun:
    def _script(
        self, monkeypatch: pytest.MonkeyPatch, events: list[Any], llm: Any = None, minted: str | None = None
    ) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        async def fake_build(self: RetrievalToolsAgent, **kwargs: Any) -> tuple[Any, Any, Any, Any, str | None]:
            captured["build_kwargs"] = kwargs
            return "graph", "messages", "config", llm, minted

        async def fake_project(agent: Any, messages: Any, config: Any) -> AsyncIterator[Any]:
            captured["project"] = (agent, messages, config)
            for event in events:
                yield event

        monkeypatch.setattr(RetrievalToolsAgent, "_build", fake_build)
        monkeypatch.setattr(ragent, "aproject_agent_events", fake_project)
        return captured

    def _final(self, result: str) -> MessageFinal:
        return MessageFinal(text=json.dumps({"status": "success", "message": "m", "result": result}))

    def test_a_run_marks_the_thread_it_minted_at_its_terminal(
        self, monkeypatch: pytest.MonkeyPatch, ledger_book: Any
    ) -> None:
        self._script(monkeypatch, [self._final("done")], minted="minted-1")
        asyncio.run(
            _collect(
                RetrievalToolsAgent().astream(user_message=TemplatedText(content="hi"), checkpoint_provider="postgres")
            )
        )
        assert ledger_book.finished("postgres") == ["minted-1"]

    def test_a_run_without_a_terminal_marks_nothing(self, monkeypatch: pytest.MonkeyPatch, ledger_book: Any) -> None:
        self._script(monkeypatch, [], minted="minted-1")
        with pytest.raises(ValueError, match="produced no terminal message"):
            asyncio.run(
                _collect(
                    RetrievalToolsAgent().astream(
                        user_message=TemplatedText(content="hi"), checkpoint_provider="postgres"
                    )
                )
            )
        assert ledger_book.finished("postgres") == []

    def test_astream_honors_thread_id_and_forwards_build_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # thread_id is an honored ABC parameter: it is mapped into the run config's
        # ``configurable`` (never dropped), while the ``_build`` parameters pass
        # through and the projection is yielded untouched.
        captured = self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()

        events = asyncio.run(
            _collect(agent.astream(user_message=TemplatedText(content="hi"), tools_limit=3, thread_id="keep-me"))
        )

        assert [type(event) for event in events] == [MessageFinal]
        assert events[0].text == "done"
        build_kwargs = captured["build_kwargs"]
        assert build_kwargs["user_message"] == TemplatedText(content="hi")
        assert build_kwargs["tools_limit"] == 3
        assert build_kwargs["config"] == {"configurable": {"thread_id": "keep-me"}}
        assert captured["project"] == ("graph", "messages", "config")

    def test_astream_forwards_user_content_kwargs_to_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # user_content_kwargs is an honored _BUILD_PARAM: it reaches ``_build``, which
        # hands it to build_agent_input to mark the user message for caching.
        captured = self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()
        cache = {"cache_control": {"type": "ephemeral"}}

        asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="hi"), user_content_kwargs=cache)))

        assert captured["build_kwargs"]["user_content_kwargs"] == cache

    def test_run_honors_thread_id_and_resume_checkpoint_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The run face drains astream, so both honored memory parameters reach the
        # same config seam: thread_id -> configurable.thread_id, resume_checkpoint_id
        # -> configurable.checkpoint_id.
        captured = self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()

        asyncio.run(agent.run(user_message=TemplatedText(content="hi"), thread_id="th", resume_checkpoint_id="cp"))

        assert captured["build_kwargs"]["config"]["configurable"] == {"thread_id": "th", "checkpoint_id": "cp"}

    def test_honored_config_overlays_a_caller_supplied_config_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # thread_id overlays onto a caller-supplied ``langgraph_config`` base — the
        # ecosystem-standard base-config name every agent honors — rather than
        # discarding it, preserving other configurable entries.
        captured = self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()

        asyncio.run(
            _collect(
                agent.astream(
                    user_message=TemplatedText(content="hi"),
                    thread_id="th",
                    langgraph_config={"configurable": {"monitoring_trace_id": "1" * 32}},
                )
            )
        )

        assert captured["build_kwargs"]["config"]["configurable"] == {
            "monitoring_trace_id": "1" * 32,
            "thread_id": "th",
        }

    @pytest.mark.parametrize(("param", "value"), _UNDECLARED_CASES)
    def test_an_undeclared_keyword_is_refused_on_both_faces(
        self, monkeypatch: pytest.MonkeyPatch, param: str, value: Any
    ) -> None:
        # Falsy-but-present values (strategy="", interrupt_on={}, resume=False) are refused too:
        # an undeclared keyword is refused by presence, never by truthiness.
        self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()
        match = rf"retrieval_tools_agent\.astream got unknown run keyword argument\(s\): {param}"
        with pytest.raises(TypeError, match=match):
            asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="hi"), **{param: value})))
        with pytest.raises(TypeError, match=match):
            asyncio.run(agent.run(user_message=TemplatedText(content="hi"), **{param: value}))

    @pytest.mark.parametrize("blank", ["", "   "])
    @pytest.mark.parametrize("key", ["thread_id", "resume_checkpoint_id"])
    def test_astream_rejects_blank_memory_key(self, monkeypatch: pytest.MonkeyPatch, key: str, blank: str) -> None:
        # A present-but-blank thread_id / resume_checkpoint_id is malformed — it would
        # silently share a checkpoint namespace across independent runs — so astream
        # raises before building rather than writing it into configurable verbatim.
        self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()
        with pytest.raises(ValueError, match=rf"retrieval_tools_agent\.astream: {key} must be a non-empty string"):
            asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="hi"), **{key: blank})))  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [123, ["x"]])
    @pytest.mark.parametrize("key", ["thread_id", "resume_checkpoint_id"])
    def test_astream_rejects_non_string_memory_key(self, monkeypatch: pytest.MonkeyPatch, key: str, value: Any) -> None:
        # A non-string thread_id / resume_checkpoint_id is a type violation — it cannot
        # name a checkpoint namespace — so astream raises TypeError naming the offending
        # param AND the received type, rather than probing a non-string for whitespace.
        self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()
        with pytest.raises(
            TypeError,
            match=rf"retrieval_tools_agent\.astream: {key} must be a string or None; got {type(value).__name__}",
        ):
            asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="hi"), **{key: value})))

    def test_astream_honors_recursion_limit_into_build_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # recursion_limit is a standard RunnableConfig key the compiled graph reads,
        # so it is overlaid onto the config astream hands to _build (which threads it
        # through init_langgraph_config to the graph invocation — see
        # test_config_util) rather than rejected. A falsy 0 is a real forwarded value.
        captured = self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()
        asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="hi"), recursion_limit=0)))
        assert captured["build_kwargs"]["config"]["recursion_limit"] == 0

    def test_run_honors_recursion_limit_into_build_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The run face drains astream, so the honored recursion_limit reaches the same
        # config seam.
        captured = self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()
        asyncio.run(agent.run(user_message=TemplatedText(content="hi"), recursion_limit=9))
        assert captured["build_kwargs"]["config"]["recursion_limit"] == 9

    def test_run_drains_astream_to_final_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._script(monkeypatch, [self._final("the answer")])
        agent = RetrievalToolsAgent()
        assert asyncio.run(agent.run(user_message=TemplatedText(content="hi"))) == "the answer"

    def test_astream_with_response_format_emits_one_structured_final(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With a response_format set, the terminal result is forced into the schema by a
        # structured finalization pass over the resolved llm. A native-capable model binds
        # the kit's response_format kwargs (never with_structured_output) and the run ends
        # with exactly one StructuredFinal (the validated object) and NO text MessageFinal.
        llm = _NativeFake(['{"value": 7}'])
        self._script(monkeypatch, [self._final("the answer")], llm=llm)
        agent = RetrievalToolsAgent()

        events = asyncio.run(
            _collect(agent.astream(user_message=TemplatedText(content="hi"), response_format=_RETRIEVAL_SCHEMA))
        )

        finals = [e for e in events if isinstance(e, StructuredFinal)]
        assert len(finals) == 1
        assert finals[0].data == {"value": 7}
        assert not any(isinstance(e, MessageFinal) for e in events)
        assert llm.bind_kwargs
        assert "response_format" in llm.bind_kwargs[0]
        assert llm.with_structured_output_called is False
        # The finalization message carries the terminal envelope's plain-text result.
        assert llm.captured["messages"][0].content == "the answer"

    def test_run_with_response_format_returns_the_structured_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _NativeFake(['{"value": 7}'])
        self._script(monkeypatch, [self._final("the answer")], llm=llm)
        agent = RetrievalToolsAgent()
        assert asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format=_RETRIEVAL_SCHEMA)) == {
            "value": 7
        }

    def test_unprofiled_finalizer_takes_the_tool_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _ToolFake([{"value": 7}])
        self._script(monkeypatch, [self._final("the answer")], llm=llm)
        agent = RetrievalToolsAgent()
        assert asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format=_RETRIEVAL_SCHEMA)) == {
            "value": 7
        }
        assert llm.with_structured_output_calls >= 1

    def test_run_response_format_without_title_raises_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._script(monkeypatch, [self._final("the answer")])
        agent = RetrievalToolsAgent()
        with pytest.raises(ValueError, match="top-level 'title'"):
            asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format={"type": "object"}))

    def test_astream_response_format_without_title_raises_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The streaming face — the one the public run door drives — rejects an
        untitled ``response_format`` up front, not after the whole run has executed
        and the finalization pass reaches langchain."""
        self._script(monkeypatch, [self._final("the answer")])
        agent = RetrievalToolsAgent()
        with pytest.raises(ValueError, match="top-level 'title'"):
            asyncio.run(
                _collect(agent.astream(user_message=TemplatedText(content="hi"), response_format={"type": "object"}))
            )

    def test_run_response_format_nonconforming_then_conforming_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A finalization payload violating a schema constraint keyword is re-prompted
        # in-node; the second, conforming answer is returned.
        schema = {
            "title": "Answer",
            "type": "object",
            "properties": {"value": {"type": "integer", "minimum": 0}},
            "required": ["value"],
        }
        llm = _NativeFake(['{"value": -1}', '{"value": 3}'])
        self._script(monkeypatch, [self._final("the answer")], llm=llm)
        agent = RetrievalToolsAgent()
        assert asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format=schema)) == {"value": 3}
        assert llm.calls == 2

    def test_run_response_format_never_conforming_ends_on_unresolved_final(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A finalization that never conforms ends on the typed outcome from the face,
        # never a raw raise.
        from tai42_contract.agent.events import StructuredOutputUnresolvedFinal

        schema = {
            "title": "Answer",
            "type": "object",
            "properties": {"value": {"type": "integer", "minimum": 0}},
            "required": ["value"],
        }
        llm = _NativeFake(['{"value": -1}'])
        self._script(monkeypatch, [self._final("the answer")], llm=llm)
        agent = RetrievalToolsAgent()
        result = asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format=schema))
        assert isinstance(result, StructuredOutputUnresolvedFinal)


class TestInputModel:
    def test_input_rejects_unknown_key(self) -> None:
        # ``extra="forbid"`` turns a typo at the run door into a loud validation
        # error rather than a silently ignored field.
        with pytest.raises(ValidationError):
            RetrievalToolsAgentInput.model_validate({"user_message": {"content": "hi"}, "unknown_key": 1})

    def test_input_advertises_response_format(self) -> None:
        # ``response_format`` is an advertised, honored field (round-trips through the
        # tool schema for the preset/authoring path).
        assert "response_format" in RetrievalToolsAgentInput.model_json_schema()["properties"]
        parsed = RetrievalToolsAgentInput.model_validate(
            {"user_message": {"content": "hi"}, "response_format": _RETRIEVAL_SCHEMA}
        )
        assert parsed.response_format == _RETRIEVAL_SCHEMA

    def test_empty_content_kwargs_normalize_to_none(self) -> None:
        # An empty ``user_content_kwargs`` dict from the JSON door reads as absent — the
        # builders treat {} as no mark, so the field normalizes to None rather than a
        # set-but-empty value the unhonored-reject face would misread.
        validated = RetrievalToolsAgentInput.model_validate(
            {"user_message": {"content": "hi"}, "user_content_kwargs": {}}
        )
        assert validated.user_content_kwargs is None
        # A non-empty mark is a real value and rides through unchanged.
        marked = RetrievalToolsAgentInput.model_validate(
            {"user_message": {"content": "hi"}, "user_content_kwargs": {"cache_control": {"type": "ephemeral"}}}
        )
        assert marked.user_content_kwargs == {"cache_control": {"type": "ephemeral"}}
