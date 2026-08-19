"""RollingCacheMarkMiddleware: keep only the newest user-side ``cache_control``
breakpoint at the model call, stripping every older mark from replayed history.

The transform is pinned directly and through the ``wrap_model_call`` /
``awrap_model_call`` hooks, which prove the rewrite is request-scoped: the handler
sees the stripped messages while the original state messages stay untouched (the
mark is never removed from checkpoint state, so each turn re-rolls it).
"""

import pytest

pytest.importorskip("langgraph")

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from tai42_kit.llm.middleware.rolling_cache_mark import RollingCacheMarkMiddleware, roll_cache_marks

_EPHEMERAL = {"type": "ephemeral"}


def _marked(text: str, msg_id: str) -> HumanMessage:
    return HumanMessage(content=[{"type": "text", "text": text, "cache_control": _EPHEMERAL}], id=msg_id)


def _request(messages: list[AnyMessage]) -> ModelRequest:
    # The model is never invoked by the middleware; a sentinel satisfies the field.
    return ModelRequest(model=object(), messages=messages)  # type: ignore[arg-type]


def _capturing_handler(seen: list[list[AnyMessage]]):
    def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request.messages)
        return ModelResponse(result=[AIMessage(content="ok")])

    return handler


def test_multi_mark_keeps_only_the_last():
    older = _marked("older", "1")
    reply = AIMessage(content="reply", id="2")
    newest = _marked("newest", "3")

    rolled = roll_cache_marks([older, reply, newest])

    assert rolled is not None
    # The older mark's single text block collapses to a plain string with no mark.
    assert rolled[0].content == "older"
    # The AIMessage between them is shared by reference, unchanged.
    assert rolled[1] is reply
    # The newest mark survives untouched, so exactly one breakpoint goes out.
    assert rolled[2] is newest
    assert rolled[2].content == [{"type": "text", "text": "newest", "cache_control": _EPHEMERAL}]


def test_single_mark_is_inert():
    reply = AIMessage(content="reply", id="1")
    only = _marked("only", "2")
    assert roll_cache_marks([reply, only]) is None


def test_no_marks_is_inert():
    assert roll_cache_marks([HumanMessage("hi", id="1"), AIMessage("there", id="2")]) is None


def test_non_text_blocks_are_untouched():
    # A non-text block WITHOUT a mark stays verbatim; a marked non-text block earlier
    # in history keeps its non-cache_control keys, losing only the mark.
    image = {"type": "image", "source": {"url": "x"}, "cache_control": _EPHEMERAL}
    older = HumanMessage(content=[image], id="1")
    newest = _marked("newest", "2")

    rolled = roll_cache_marks([older, newest])

    assert rolled is not None
    # The image block is not a bare text block, so it stays a dict, mark removed.
    assert rolled[0].content == [{"type": "image", "source": {"url": "x"}}]
    assert rolled[1] is newest


def test_mark_on_tool_and_ai_messages_is_rolled():
    tool = ToolMessage(
        content=[{"type": "text", "text": "tool out", "cache_control": _EPHEMERAL}],
        tool_call_id="c1",
        id="1",
    )
    ai = AIMessage(content=[{"type": "text", "text": "step", "cache_control": _EPHEMERAL}], id="2")
    newest = _marked("newest", "3")

    rolled = roll_cache_marks([tool, ai, newest])

    assert rolled is not None
    assert rolled[0].content == "tool out"
    assert rolled[1].content == "step"
    assert rolled[2] is newest


def test_wrap_model_call_strips_transiently_leaving_state_untouched():
    mw = RollingCacheMarkMiddleware()
    older = _marked("older", "1")
    newest = _marked("newest", "2")
    messages = [older, newest]
    request = _request(messages)

    seen: list[list[AnyMessage]] = []
    mw.wrap_model_call(request, _capturing_handler(seen))

    # The handler (the model call) saw the older mark stripped to a plain string.
    assert seen[0][0].content == "older"
    assert seen[0][1] is newest
    # The state messages the request was built from are NOT mutated: the older
    # message still carries its mark, so the next turn re-rolls from the same history.
    assert older.content == [{"type": "text", "text": "older", "cache_control": _EPHEMERAL}]
    assert messages[0] is older


async def test_awrap_model_call_matches_the_sync_path():
    mw = RollingCacheMarkMiddleware()
    older = _marked("older", "1")
    newest = _marked("newest", "2")
    request = _request([older, newest])

    seen: list[list[AnyMessage]] = []

    async def handler(req: ModelRequest) -> ModelResponse:
        seen.append(req.messages)
        return ModelResponse(result=[AIMessage(content="ok")])

    await mw.awrap_model_call(request, handler)

    assert seen[0][0].content == "older"
    assert seen[0][1] is newest
    assert older.content == [{"type": "text", "text": "older", "cache_control": _EPHEMERAL}]


def test_wrap_model_call_inert_passes_request_through():
    mw = RollingCacheMarkMiddleware()
    only = _marked("only", "1")
    request = _request([only])

    seen: list[list[AnyMessage]] = []
    mw.wrap_model_call(request, _capturing_handler(seen))

    # Nothing to roll: the same request object flows to the handler untouched.
    assert seen[0] is request.messages
    assert seen[0][0] is only
