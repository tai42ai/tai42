"""``retrieval_tools_agent`` build, the ``astream`` and ``run`` faces with the
reject-unhonored guards, and the input model.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.agent import (
    Agent,
    MessageFinal,
    StructuredFinal,
)
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError
from tests._retrieval_tools_agent_support import (
    _RETRIEVAL_SCHEMA,
    _BoomStructuredLLM,
    _collect,
    _patch_build_seams,
    _StructuredLLM,
)

from tai42_agents._internal.reject import reject_unhonored
from tai42_agents.retrieval_tools_agent import agent as ragent
from tai42_agents.retrieval_tools_agent.agent import (
    _UNHONORED_REASONS,
    RetrievalToolsAgent,
    RetrievalToolsAgentInput,
)


class TestBuild:
    def test_resolves_defaults_and_returns_agent_messages_config(
        self, monkeypatch: pytest.MonkeyPatch, resource_manager: Any
    ) -> None:
        captured = _patch_build_seams(monkeypatch)
        agent = RetrievalToolsAgent()

        compiled, messages, config, llm = asyncio.run(
            agent._build(
                system_message=TemplatedText(content="be brief"),
                user_message=TemplatedText(content="do it"),
                tools_limit=7,
            )
        )

        assert compiled is captured["compiled"]
        assert config == {"configurable": {"thread_id": "t"}}
        # ``_build`` hands back the resolved llm for the structured finalization pass.
        assert llm == "llm-obj"
        # The rendered user message is the whole agent input; the system prompt is
        # per-run graph configuration (prepended at each model call, never
        # checkpointed state) and reaches the graph constructor instead.
        assert messages == {"messages": [{"role": "user", "content": "do it"}]}
        assert "be brief" in captured["graph_kwargs"]["system_prompt"]

        # Providers fell back to the settings defaults and were threaded through.
        assert captured["llm_provider"] == "def_llm"
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
                llm_provider="my_llm",
                embedding_provider="my_embedding",
                checkpoint_provider="my_checkpoint",
                store_provider="my_store",
            )
        )

        assert captured["llm_provider"] == "my_llm"
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


# Every key in ``_UNHONORED_REASONS`` paired with a representative SET value; the
# falsy-but-present scalars (strategy="", interrupt_on={}, resume=False) pin that a
# scalar is set whenever it is not None. Distinct params here must equal the full map.
_UNHONORED_CASES = [
    ("subagents", [object()]),
    ("strategy", "parallel"),
    ("strategy", ""),
    ("skills", ["s"]),
    ("inline_skills", [{"name": "n", "content": "c"}]),
    ("interrupt_on", {"tool": True}),
    ("interrupt_on", {}),
    ("resume", {"answer": "y"}),
    ("resume", False),
    ("system_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
]


# The unhonored params whose ABC ``Agent.run`` default is an empty collection (``()`` /
# ``""``), read from the contract signature — the independent source of truth for which
# unhonored params are collection-typed. Intersected with this agent's reasons map it is
# exactly the set ``_UNHONORED_COLLECTION_PARAMS`` must classify as collections. The
# empty-collection test below parametrizes from HERE, not from the frozenset, so a member
# dropped from the frozenset (reclassifying it as a scalar) turns a case red rather than
# silently vanishing.
_EMPTY_COLLECTION_ABC_DEFAULTS = frozenset(
    name
    for name, parameter in inspect.signature(Agent.run).parameters.items()
    if isinstance(parameter.default, (tuple, list, str)) and not parameter.default
)


_COLLECTION_REJECT_PARAMS = sorted(_UNHONORED_REASONS.keys() & _EMPTY_COLLECTION_ABC_DEFAULTS)


class TestAstreamAndRun:
    def _script(self, monkeypatch: pytest.MonkeyPatch, events: list[Any], llm: Any = None) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        async def fake_build(self: RetrievalToolsAgent, **kwargs: Any) -> tuple[Any, Any, Any, Any]:
            captured["build_kwargs"] = kwargs
            return "graph", "messages", "config", llm

        async def fake_project(agent: Any, messages: Any, config: Any) -> AsyncIterator[Any]:
            captured["project"] = (agent, messages, config)
            for event in events:
                yield event

        monkeypatch.setattr(RetrievalToolsAgent, "_build", fake_build)
        monkeypatch.setattr(ragent, "aproject_agent_events", fake_project)
        return captured

    def _final(self, result: str) -> MessageFinal:
        return MessageFinal(text=json.dumps({"status": "success", "message": "m", "result": result}))

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
        assert captured["build_kwargs"] == {
            "user_message": TemplatedText(content="hi"),
            "tools_limit": 3,
            "config": {"configurable": {"thread_id": "keep-me"}},
        }
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
                    langgraph_config={"configurable": {"monitoring_trace_id": "x"}},
                )
            )
        )

        assert captured["build_kwargs"]["config"]["configurable"] == {"monitoring_trace_id": "x", "thread_id": "th"}

    @pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
    def test_unsupported_abc_param_raises_naming_the_exact_face(
        self, monkeypatch: pytest.MonkeyPatch, param: str, value: Any
    ) -> None:
        # Every ABC parameter this runtime cannot honor raises a RuntimeError naming
        # the offending parameter AND the exact face it was called on, never a silent
        # drop. Falsy-but-present scalars (strategy="", interrupt_on={}, resume=False)
        # still raise — a scalar parameter is set whenever it is not None, so they
        # never slip through a truthiness gate. Asserting the exact face token makes
        # each face's guard load-bearing: dropping the run-face guard (which delegates
        # to astream) would surface the ``astream`` token and fail the run assertion.
        self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()

        with pytest.raises(RuntimeError, match=rf"retrieval_tools_agent\.astream does not support .*\b{param}\b"):
            asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="hi"), **{param: value})))
        with pytest.raises(RuntimeError, match=rf"retrieval_tools_agent\.run does not support .*\b{param}\b"):
            asyncio.run(agent.run(user_message=TemplatedText(content="hi"), **{param: value}))

    def test_unhonored_cases_cover_the_full_reasons_map(self) -> None:
        # Every key in the guard's reasons map has a parametrized reject case above, so
        # a key added to the map without a matching test fails here immediately.
        assert {param for param, _ in _UNHONORED_CASES} == set(_UNHONORED_REASONS)

    def test_collection_params_match_the_abc_collection_defaults(self) -> None:
        # _UNHONORED_COLLECTION_PARAMS is exactly this agent's unhonored params whose ABC
        # default is an empty collection: no scalar wrongly listed (which would let a
        # meaningful falsy value slip through), none dropped (which would over-reject the
        # not-requested empty default).
        assert set(ragent._UNHONORED_COLLECTION_PARAMS) == set(_COLLECTION_REJECT_PARAMS)

    @pytest.mark.parametrize("empty", [[], ""])
    @pytest.mark.parametrize("param", _COLLECTION_REJECT_PARAMS)
    def test_reject_unhonored_permits_empty_collection_param(self, param: str, empty: object) -> None:
        # An empty collection is the ABC's "not requested" default for a collection
        # parameter, so the guard does not raise for it — in either falsy empty form ([] /
        # ""). Were the parameter dropped from _UNHONORED_COLLECTION_PARAMS it would be
        # classified as a scalar (set whenever it is not None) and this empty value would
        # raise.
        reject_unhonored(
            "retrieval_tools_agent.run",
            {param: empty},
            _UNHONORED_REASONS,
            collection_params=ragent._UNHONORED_COLLECTION_PARAMS,
        )

    @pytest.mark.parametrize("blank", ["", "   "])
    @pytest.mark.parametrize("key", ["thread_id", "resume_checkpoint_id"])
    def test_astream_rejects_blank_memory_key(self, monkeypatch: pytest.MonkeyPatch, key: str, blank: str) -> None:
        # A present-but-blank thread_id / resume_checkpoint_id is malformed — it would
        # silently share a checkpoint namespace across independent runs — so astream
        # raises before building rather than writing it into configurable verbatim.
        self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()
        with pytest.raises(ValueError, match=rf"retrieval_tools_agent\.astream: {key} must be a non-empty string"):
            asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="hi"), **{key: blank})))

    @pytest.mark.parametrize("blank", ["", "   "])
    @pytest.mark.parametrize("key", ["thread_id", "resume_checkpoint_id"])
    def test_run_rejects_blank_memory_key(self, monkeypatch: pytest.MonkeyPatch, key: str, blank: str) -> None:
        # Parity with astream: the run face rejects a present-but-blank memory key loudly.
        self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()
        with pytest.raises(ValueError, match=rf"retrieval_tools_agent\.run: {key} must be a non-empty string"):
            asyncio.run(agent.run(user_message=TemplatedText(content="hi"), **{key: blank}))

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

    @pytest.mark.parametrize("value", [123, ["x"]])
    @pytest.mark.parametrize("key", ["thread_id", "resume_checkpoint_id"])
    def test_run_rejects_non_string_memory_key(self, monkeypatch: pytest.MonkeyPatch, key: str, value: Any) -> None:
        # Parity with astream: the run face rejects a non-string memory key with a
        # TypeError naming the offending param and the received type.
        self._script(monkeypatch, [self._final("done")])
        agent = RetrievalToolsAgent()
        with pytest.raises(
            TypeError, match=rf"retrieval_tools_agent\.run: {key} must be a string or None; got {type(value).__name__}"
        ):
            asyncio.run(agent.run(user_message=TemplatedText(content="hi"), **{key: value}))

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
        # With a response_format set, the terminal result is forced into the schema
        # by a structured finalization pass over the resolved llm: the run ends with
        # exactly one StructuredFinal (carrying the validated object) and NO text
        # MessageFinal, and the schema is passed straight to the provider.
        llm = _StructuredLLM({"value": 7})
        self._script(monkeypatch, [self._final("the answer")], llm=llm)
        agent = RetrievalToolsAgent()

        events = asyncio.run(
            _collect(agent.astream(user_message=TemplatedText(content="hi"), response_format=_RETRIEVAL_SCHEMA))
        )

        finals = [e for e in events if isinstance(e, StructuredFinal)]
        assert len(finals) == 1
        assert finals[0].data == {"value": 7}
        assert not any(isinstance(e, MessageFinal) for e in events)
        assert llm.captured["schema"] == _RETRIEVAL_SCHEMA
        assert llm.captured["include_raw"] is False
        # The finalization message carries the terminal envelope's plain-text result.
        assert llm.captured["messages"][0].content == "the answer"

    def test_run_with_response_format_returns_the_structured_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _StructuredLLM({"value": 7})
        self._script(monkeypatch, [self._final("the answer")], llm=llm)
        agent = RetrievalToolsAgent()
        assert asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format=_RETRIEVAL_SCHEMA)) == {
            "value": 7
        }

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

    def test_run_response_format_unparseable_finalization_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unparseable structured-finalization output propagates the raise loudly —
        # never silence, never a text fallback.
        llm = _BoomStructuredLLM()
        self._script(monkeypatch, [self._final("the answer")], llm=llm)
        agent = RetrievalToolsAgent()
        with pytest.raises(ValueError, match="unparseable structured output"):
            asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format=_RETRIEVAL_SCHEMA))

    def test_run_response_format_nonconforming_structured_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A finalization payload violating a schema constraint keyword (minimum)
        # raises loudly from the validation step instead of being returned.
        schema = {
            "title": "Answer",
            "type": "object",
            "properties": {"value": {"type": "integer", "minimum": 0}},
            "required": ["value"],
        }
        llm = _StructuredLLM({"value": -1})
        self._script(monkeypatch, [self._final("the answer")], llm=llm)
        agent = RetrievalToolsAgent()
        with pytest.raises(JsonSchemaValidationError):
            asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format=schema))


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
