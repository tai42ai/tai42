"""LeadingUserMiddleware: the outgoing request opens with a user turn; the thread never stores the marker.

The rule (:func:`lead_with_user`) is pinned directly and through the ``wrap_model_call`` /
``awrap_model_call`` hooks: the handler sees the led messages while the request's own messages
stay untouched, so nothing reaches checkpoint state.
"""

import pytest

pytest.importorskip("langgraph")

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from tai42_kit.llm.middleware.leading_user import _CONVERSATION_START_MARKER, LeadingUserMiddleware, lead_with_user


def _request(messages: list[AnyMessage]) -> ModelRequest:
    # The model is never invoked by the middleware; a sentinel satisfies the field.
    return ModelRequest(model=object(), messages=messages)  # type: ignore[arg-type]


def _capturing_handler(seen: list[list[AnyMessage]]):
    def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request.messages)
        return ModelResponse(result=[AIMessage(content="ok")])

    return handler


def test_leading_assistant_gains_a_marker_turn():
    ai = AIMessage("operator opener", id="a")
    human = HumanMessage("client reply", id="h")
    led = lead_with_user([ai, human])

    assert led is not None
    assert isinstance(led[0], HumanMessage)
    assert led[0].content == _CONVERSATION_START_MARKER
    assert led[1] is ai
    assert led[2] is human


def test_user_first_is_untouched():
    msgs: list[AnyMessage] = [HumanMessage("hi", id="1"), AIMessage("there", id="2")]
    assert lead_with_user(msgs) is None


def test_system_then_assistant_inserts_after_system():
    system = SystemMessage("rules", id="s")
    ai = AIMessage("operator opener", id="a")
    led = lead_with_user([system, ai])

    assert led is not None
    assert led[0] is system
    assert isinstance(led[1], HumanMessage)
    assert led[1].content == _CONVERSATION_START_MARKER
    assert led[2] is ai


def test_empty_history_is_untouched():
    assert lead_with_user([]) is None


def test_wrap_model_call_leads_the_request_and_leaves_its_messages_alone():
    ai = AIMessage("operator opener", id="a")
    original: list[AnyMessage] = [ai]
    seen: list[list[AnyMessage]] = []
    LeadingUserMiddleware().wrap_model_call(_request(original), _capturing_handler(seen))

    assert [m.content for m in seen[0]] == [_CONVERSATION_START_MARKER, "operator opener"]
    assert original == [ai]


def test_wrap_model_call_passes_a_user_first_request_through():
    original: list[AnyMessage] = [HumanMessage("hi", id="h")]
    request = _request(original)
    seen: list[list[AnyMessage]] = []
    LeadingUserMiddleware().wrap_model_call(request, _capturing_handler(seen))
    assert seen[0] is original


async def test_awrap_model_call_leads_the_request():
    ai = AIMessage("operator opener", id="a")
    seen: list[list[AnyMessage]] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request.messages)
        return ModelResponse(result=[AIMessage(content="ok")])

    await LeadingUserMiddleware().awrap_model_call(_request([ai]), handler)
    assert [m.content for m in seen[0]] == [_CONVERSATION_START_MARKER, "operator opener"]
