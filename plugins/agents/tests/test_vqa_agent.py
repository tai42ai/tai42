"""Tests for the ``vqa_agent`` module.

Cover registration through the real decorator, the hand-rolled ``astream``
projection (token deltas → assembled final → summed usage), ``run`` draining
that stream to the final text with no app facets touched, and ``ToolInput``
validation. A scripted fake LLM whose ``astream`` yields ``AIMessageChunk``s
stands in for a real provider — no network, no live model.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import ValidationError
from tai42_contract.agent import Agent
from tai42_contract.agent.events import MessageDelta, MessageFinal, RunUsage, StreamEvent
from tai42_contract.app import tai42_app

import tai42_agents.vqa_agent as vqa
from tai42_agents._internal.text import text_of
from tai42_agents.vqa_agent import VqaAgent, VqaAgentInput


class _FakeLLM:
    """A stand-in chat model whose ``astream`` yields two scripted chunks. The
    usage is split across both chunks so an assertion on ``total_tokens`` passes
    only when the accumulator sums them (a keep-last accumulator would give 2)."""

    async def astream(self, _messages: object, config: object = None) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="Hel",
            usage_metadata={"input_tokens": 3, "output_tokens": 0, "total_tokens": 3},
        )
        yield AIMessageChunk(
            content="lo",
            usage_metadata={"input_tokens": 0, "output_tokens": 2, "total_tokens": 2},
        )


class _Settings:
    def with_fallbacks(self, _user_config: object) -> dict[str, object]:
        return {}


class _ProviderSettings:
    llm = "fake"


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the module's llm factory + settings at the scripted fake."""

    async def fake_get_llm(provider: str, **_kwargs: object) -> _FakeLLM:
        assert provider == "fake"
        return _FakeLLM()

    monkeypatch.setattr(vqa, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(vqa, "llm_settings", lambda: _Settings())
    monkeypatch.setattr(vqa, "llm_provider_settings", lambda: _ProviderSettings())


async def _collect(agen: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [event async for event in agen]


class _ScriptedLLM:
    """A stand-in chat model whose ``astream`` replays a fixed list of chunks — an
    empty list is an empty stream."""

    def __init__(self, chunks: list[AIMessageChunk]) -> None:
        self._chunks = chunks

    async def astream(self, _messages: object, config: object = None) -> AsyncIterator[AIMessageChunk]:
        for chunk in self._chunks:
            yield chunk


def _install_scripted_llm(monkeypatch: pytest.MonkeyPatch, chunks: list[AIMessageChunk]) -> None:
    """Point the module's llm factory + settings at a fake replaying ``chunks``."""

    async def fake_get_llm(provider: str, **_kwargs: object) -> _ScriptedLLM:
        assert provider == "fake"
        return _ScriptedLLM(chunks)

    monkeypatch.setattr(vqa, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(vqa, "llm_settings", lambda: _Settings())
    monkeypatch.setattr(vqa, "llm_provider_settings", lambda: _ProviderSettings())


def test_decorator_registers_a_live_instance() -> None:
    agent = tai42_app.agents.get_agent("vqa_agent")
    assert isinstance(agent, VqaAgent)
    assert isinstance(agent, Agent)
    assert agent.tool_name == "vqa_agent"
    assert agent.ToolInput is VqaAgentInput


def test_astream_projects_the_llm_stream(fake_llm: None) -> None:
    events = asyncio.run(_collect(VqaAgent().astream(image_url="http://img", query="describe")))

    assert [event.type for event in events] == [
        "message_delta",
        "message_delta",
        "run_usage",
        "message_final",
    ]
    assert [event.text for event in events if isinstance(event, MessageDelta)] == ["Hel", "lo"]
    final = next(event for event in events if isinstance(event, MessageFinal))
    assert final.text == "Hello"
    assert final.final is True
    usage = next(event for event in events if isinstance(event, RunUsage))
    assert usage.input_tokens == 3
    assert usage.output_tokens == 2
    assert usage.total_tokens == 5


def test_run_drains_to_the_final_text(fake_llm: None) -> None:
    result = asyncio.run(VqaAgent().run(image_url="http://img", query="describe"))
    assert result == "Hello"


def test_astream_empty_stream_yields_nothing_and_run_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty model stream is an empty run: nothing accumulates, so no usage and no
    final are emitted, and draining it returns ``''`` — the documented empty-run result,
    never a partial."""
    _install_scripted_llm(monkeypatch, [])
    assert asyncio.run(_collect(VqaAgent().astream(image_url="http://img", query="describe"))) == []
    assert asyncio.run(VqaAgent().run(image_url="http://img", query="describe")) == ""


def test_astream_empty_text_chunk_yields_no_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunk whose decoded text is empty contributes no ``MessageDelta``: only the
    accumulated usage surfaces, and the empty answer yields no final."""
    _install_scripted_llm(
        monkeypatch,
        [AIMessageChunk(content="", usage_metadata={"input_tokens": 4, "output_tokens": 0, "total_tokens": 4})],
    )
    events = asyncio.run(_collect(VqaAgent().astream(image_url="http://img", query="describe")))
    assert [event.type for event in events] == ["run_usage"]
    usage = next(event for event in events if isinstance(event, RunUsage))
    assert usage.total_tokens == 4


def test_astream_chunk_without_usage_emits_no_run_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the provider reports no token counts, ``usage_event`` returns ``None`` and the
    stream omits ``RunUsage`` — an honest omission — while the delta and the final for the
    answer text still flow."""
    _install_scripted_llm(monkeypatch, [AIMessageChunk(content="Hi")])
    events = asyncio.run(_collect(VqaAgent().astream(image_url="http://img", query="describe")))
    assert [event.type for event in events] == ["message_delta", "message_final"]
    final = next(event for event in events if isinstance(event, MessageFinal))
    assert final.text == "Hi"


def test_astream_whitespace_only_answer_yields_no_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only answer strips to empty, so no ``MessageFinal`` is emitted: the
    delta carrying the whitespace still flows, but the run has no terminal."""
    _install_scripted_llm(monkeypatch, [AIMessageChunk(content="   ")])
    events = asyncio.run(_collect(VqaAgent().astream(image_url="http://img", query="describe")))
    assert [event.type for event in events] == ["message_delta"]
    assert all(not isinstance(event, MessageFinal) for event in events)


def test_vqa_message_normalizes_a_url_through_the_handle(resource_manager: Any) -> None:
    """A public URL is normalized via the ``resource_manager`` handle (awaited) and
    passes through as the image content part, alongside the text query."""
    message = asyncio.run(vqa._vqa_message("http://img/cat.png", "describe"))
    assert message.content == [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "http://img/cat.png"}},
    ]
    assert resource_manager.media_calls == ["http://img/cat.png"]


def test_vqa_message_normalizes_a_storage_id_through_the_handle(resource_manager: Any) -> None:
    """A storage id is resolved via the same handle to a base64 data-URI image
    content part — proving VqaAgent works with stored images, not just URLs."""
    message = asyncio.run(vqa._vqa_message("skills/cat.png", "describe"))
    content = message.content
    assert isinstance(content, list)
    text_part, image_part = content
    assert text_part == {"type": "text", "text": "describe"}
    assert isinstance(image_part, dict)
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/")
    assert resource_manager.media_calls == ["skills/cat.png"]


def test_run_rejects_non_image_media_through_the_handle(fake_llm: None, resource_manager: Any) -> None:
    """A resolvable non-image source (a ``.pdf``) is rejected by the resource
    manager handle's image-only guard, and the vqa media path propagates that
    ``ValueError`` rather than sending a non-image to the model. Draining ``run``
    exercises the same ``astream`` media path."""
    with pytest.raises(ValueError, match="image"):
        asyncio.run(VqaAgent().run(image_url="notes.pdf", query="describe"))
    assert resource_manager.media_calls == ["notes.pdf"]


_VQA_SCHEMA = {"title": "Answer", "type": "object", "properties": {"answer": {"type": "string"}}}
_CONSTRAINED_SCHEMA = {
    "title": "Answer",
    "type": "object",
    "properties": {"answer": {"type": "string", "minLength": 1}},
    "required": ["answer"],
}


class _NativeFake:
    """A profiled fake: declares native structured output, records each ``bind`` kwarg set,
    and answers scripted JSON text. ``with_structured_output`` must never be called on it."""

    profile = {"structured_output": True}  # noqa: RUF012

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.calls = 0
        self.bind_kwargs: list[dict[str, Any]] = []
        self.with_structured_output_called = False

    def bind(self, **kwargs: Any) -> Any:
        self.bind_kwargs.append(kwargs)
        outer = self

        class _Runner:
            async def ainvoke(self, _messages: Any, _config: object = None) -> Any:
                index = min(outer.calls, len(outer._texts) - 1)
                outer.calls += 1
                return AIMessage(content=outer._texts[index])

        return _Runner()

    def with_structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        self.with_structured_output_called = True
        raise AssertionError("native plan must not call with_structured_output")


class _ToolFake:
    """An unprofiled fake: no native output, so the plan takes the tool tier.
    ``with_structured_output(method='function_calling', include_raw=True)`` returns a scripted
    ``{raw, parsed, parsing_error}`` per call."""

    profile = None

    def __init__(self, parsed_seq: list[Any]) -> None:
        self._seq = parsed_seq
        self.calls = 0
        self.with_structured_output_calls = 0

    def with_structured_output(self, schema: Any, *, method: str | None = None, include_raw: bool = False) -> Any:
        self.with_structured_output_calls += 1
        outer = self

        class _Runner:
            async def ainvoke(self, _messages: Any, _config: object = None) -> dict[str, Any]:
                index = min(outer.calls, len(outer._seq) - 1)
                outer.calls += 1
                parsed = outer._seq[index]
                raw = AIMessage(content="", tool_calls=[{"id": "c", "name": "Answer", "args": parsed}])
                return {"raw": raw, "parsed": parsed, "parsing_error": None}

        return _Runner()


def _install_llm(monkeypatch: pytest.MonkeyPatch, llm: Any, provider: str = "openai") -> None:
    async def fake_get_llm(provider: str, **_kwargs: object) -> Any:
        return llm

    monkeypatch.setattr(vqa, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(vqa, "llm_settings", lambda: _Settings())
    monkeypatch.setattr(vqa, "llm_provider_settings", lambda: SimpleNamespace(llm=provider))


def test_astream_native_binds_kwargs_and_emits_one_structured_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """A native-capable model binds the kit's response_format kwargs (never
    ``with_structured_output``) and the completion emits exactly one StructuredFinal."""
    from tai42_contract.agent.events import StructuredFinal

    llm = _NativeFake(['{"answer": "a cat"}'])
    _install_llm(monkeypatch, llm)

    events = asyncio.run(
        _collect(VqaAgent().astream(image_url="http://img", query="describe", response_format=_VQA_SCHEMA))
    )
    assert [type(event) for event in events] == [StructuredFinal]
    final = events[0]
    assert isinstance(final, StructuredFinal)
    assert final.data == {"answer": "a cat"}
    assert llm.bind_kwargs
    assert "response_format" in llm.bind_kwargs[0]
    assert llm.with_structured_output_called is False


def test_run_native_returns_the_structured_object(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _NativeFake(['{"answer": "a cat"}'])
    _install_llm(monkeypatch, llm)
    result = asyncio.run(VqaAgent().run(image_url="http://img", query="describe", response_format=_VQA_SCHEMA))
    assert result == {"answer": "a cat"}


def test_native_nonconforming_then_conforming_succeeds_in_two_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _NativeFake(['{"answer": ""}', '{"answer": "ok"}'])
    _install_llm(monkeypatch, llm)
    result = asyncio.run(VqaAgent().run(image_url="http://img", query="describe", response_format=_CONSTRAINED_SCHEMA))
    assert result == {"answer": "ok"}
    assert llm.calls == 2


def test_native_never_conforming_ends_on_unresolved_final(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_contract.agent.events import StructuredOutputUnresolvedFinal

    llm = _NativeFake(['{"answer": ""}'])
    _install_llm(monkeypatch, llm)
    result = asyncio.run(VqaAgent().run(image_url="http://img", query="describe", response_format=_CONSTRAINED_SCHEMA))
    assert isinstance(result, StructuredOutputUnresolvedFinal)


def test_unprofiled_model_takes_the_tool_path(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _ToolFake([{"answer": "a cat"}])
    _install_llm(monkeypatch, llm)
    result = asyncio.run(VqaAgent().run(image_url="http://img", query="describe", response_format=_VQA_SCHEMA))
    assert result == {"answer": "a cat"}
    assert llm.with_structured_output_calls >= 1


def test_tool_path_nonconforming_then_conforming_succeeds_in_two_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _ToolFake([{"answer": ""}, {"answer": "ok"}])
    _install_llm(monkeypatch, llm)
    result = asyncio.run(VqaAgent().run(image_url="http://img", query="describe", response_format=_CONSTRAINED_SCHEMA))
    assert result == {"answer": "ok"}
    assert llm.calls == 2


def test_tool_path_never_conforming_ends_on_unresolved_final(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_contract.agent.events import StructuredOutputUnresolvedFinal

    llm = _ToolFake([{"answer": ""}])
    _install_llm(monkeypatch, llm)
    result = asyncio.run(VqaAgent().run(image_url="http://img", query="describe", response_format=_CONSTRAINED_SCHEMA))
    assert isinstance(result, StructuredOutputUnresolvedFinal)


def test_run_response_format_without_title_raises_loudly(fake_llm: None) -> None:
    """A ``response_format`` JSON-Schema dict lacking a top-level ``"title"`` is
    rejected loudly at the run seam."""
    with pytest.raises(ValueError, match="top-level 'title'"):
        asyncio.run(VqaAgent().run(image_url="http://img", query="describe", response_format={"type": "object"}))


def test_astream_response_format_without_title_raises_loudly(fake_llm: None) -> None:
    """The streaming face — the one the public run door drives — rejects an untitled
    ``response_format`` up front, exactly as the invoke face does."""
    with pytest.raises(ValueError, match="top-level 'title'"):
        asyncio.run(
            _collect(VqaAgent().astream(image_url="http://img", query="describe", response_format={"type": "object"}))
        )


def test_native_oversized_int_is_reprompted_then_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """An integer past the platform int64 range at an integer-typed site is well-formed JSON but
    non-conforming: the int64 bound injected onto the dict schema's integer site re-prompts it under
    the cap, and a model that keeps returning it ends on the typed outcome."""
    from tai42_contract.agent.events import StructuredOutputUnresolvedFinal

    schema = {
        "title": "Answer",
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    llm = _NativeFake(['{"count": 9223372036854775808}'])
    _install_llm(monkeypatch, llm)
    result = asyncio.run(VqaAgent().run(image_url="http://img", query="describe", response_format=schema))
    assert isinstance(result, StructuredOutputUnresolvedFinal)


# The contract keys a single completion cannot honor, each with a set value (an empty
# ``user_message`` is set: it is a scalar, refused whenever it is not ``None``).
_UNHONORED_CASES = [
    ("tool_names", ["x"]),
    ("user_message", "usr"),
    ("user_message", ""),
    ("thread_id", "t"),
]

# Keywords this agent does not declare, refused by presence.
_UNDECLARED_CASES = [
    ("tools", [object()]),
    ("system_message", "sys"),
    ("resume", False),
    ("recursion_limit", 0),
    ("checkpoint_provider", "redis"),
    ("user_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
]


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_run_refuses_a_contract_key_it_cannot_honor(fake_llm: None, param: str, value: Any) -> None:
    with pytest.raises(RuntimeError, match=rf"vqa_agent\.run does not support .*\b{param}\b"):
        asyncio.run(VqaAgent().run(image_url="http://img", query="describe", **{param: value}))


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_astream_refuses_a_contract_key_it_cannot_honor(fake_llm: None, param: str, value: Any) -> None:
    with pytest.raises(RuntimeError, match=rf"vqa_agent\.astream does not support .*\b{param}\b"):
        asyncio.run(_collect(VqaAgent().astream(image_url="http://img", query="describe", **{param: value})))


@pytest.mark.parametrize(("param", "value"), _UNDECLARED_CASES)
def test_both_faces_refuse_an_undeclared_keyword(fake_llm: None, param: str, value: Any) -> None:
    with pytest.raises(TypeError, match=rf"vqa_agent\.run got unknown run keyword argument\(s\): {param}"):
        asyncio.run(VqaAgent().run(image_url="http://img", query="describe", **{param: value}))
    with pytest.raises(TypeError, match=rf"vqa_agent\.astream got unknown run keyword argument\(s\): {param}"):
        asyncio.run(_collect(VqaAgent().astream(image_url="http://img", query="describe", **{param: value})))


def test_the_not_requested_sentinels_pass_the_guard(fake_llm: None) -> None:
    result = asyncio.run(
        VqaAgent().run(
            image_url="http://img",
            query="describe",
            thread_id=None,
            user_message=None,
            tool_names=(),
            response_format=None,
        )
    )
    assert result == "Hello"


def test_honored_params_reach_the_llm_seam_on_both_faces(monkeypatch: pytest.MonkeyPatch) -> None:
    """``llm_provider`` and ``llm_kwargs`` are honored: they reach the llm factory
    seam identically on the run and astream faces."""
    captured: dict[str, object] = {}

    async def capture_get_llm(provider: str, **kwargs: object) -> _FakeLLM:
        captured["provider"] = provider
        captured["kwargs"] = kwargs
        return _FakeLLM()

    class _CaptureSettings:
        def with_fallbacks(self, user_config: object) -> dict[str, object]:
            captured["fallbacks"] = user_config
            return {"merged": True}

    monkeypatch.setattr(vqa, "get_llm_async", capture_get_llm)
    monkeypatch.setattr(vqa, "llm_settings", lambda: _CaptureSettings())
    monkeypatch.setattr(vqa, "llm_provider_settings", lambda: _ProviderSettings())

    stream = VqaAgent().astream(image_url="http://img", query="q", llm_provider="fake", llm_kwargs={"temperature": 0.1})
    asyncio.run(_collect(stream))
    assert captured["provider"] == "fake"
    assert captured["fallbacks"] == {"temperature": 0.1}
    assert captured["kwargs"] == {"merged": True}

    captured.clear()
    asyncio.run(VqaAgent().run(image_url="http://img", query="q", llm_provider="fake", llm_kwargs={"temperature": 0.1}))
    assert captured["provider"] == "fake"
    assert captured["fallbacks"] == {"temperature": 0.1}
    assert captured["kwargs"] == {"merged": True}


def test_tool_input_rejects_unknown_key() -> None:
    """``extra="forbid"`` rejects an unknown key loudly rather than dropping a typo."""
    with pytest.raises(ValidationError):
        VqaAgentInput(image_url="http://img", query="describe", llm_providerr="oops")  # type: ignore[call-arg]


def test_tool_input_requires_image_url_and_query() -> None:
    valid = VqaAgentInput(image_url="http://img", query="describe")
    assert valid.image_url == "http://img"
    assert valid.query == "describe"
    assert valid.llm_provider is None
    assert valid.llm_kwargs is None

    with pytest.raises(ValidationError):
        VqaAgentInput(image_url="http://img")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        VqaAgentInput(query="describe")  # type: ignore[call-arg]


class TestTextOf:
    """The shared ``text_of`` helper (``tai42_agents._internal.text``) that both the
    vqa stream and the tools-agent projection use to decode message text."""

    def test_string_content_returned_verbatim(self) -> None:
        assert text_of(SimpleNamespace(content="hi")) == "hi"

    def test_list_content_joins_text_blocks_and_skips_non_text(self) -> None:
        message = SimpleNamespace(
            content=[
                "a",  # a bare string block
                {"type": "text", "text": "b"},  # an explicit text block
                {"text": "c"},  # type omitted -> treated as text
                {"type": "image_url", "image_url": {"url": "http://img"}},  # skipped
            ]
        )
        assert text_of(message) == "abc"

    def test_missing_content_is_empty(self) -> None:
        assert text_of(object()) == ""

    def test_non_str_non_list_content_is_empty(self) -> None:
        assert text_of(SimpleNamespace(content=123)) == ""
