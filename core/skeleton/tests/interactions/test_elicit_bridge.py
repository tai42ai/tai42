"""Elicitation bridge: a tool's ``ctx.elicit()`` from an
in-process caller resolves through the interactions ``ask`` channel, with
schema fidelity, accept-or-raise, and no silent dead-end. The accepted result carries the
message the answering reply quoted under the ``_meta`` key ``tai42/reply_to``."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.server.context import Context, set_context
from fastmcp.server.elicitation import AcceptedElicitation
from pydantic import BaseModel
from tai42_contract.conversations import ReplyTo, ReplyToStatus
from tai42_contract.interactions import ELICIT_META_REPLY_TO, AskAnswer

from tai42_skeleton.app.instance import app
from tai42_skeleton.interactions.elicit_bridge import BridgedAcceptedElicitation
from tai42_skeleton.interactions.helper import InteractionTimeoutError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools import context_bridge


def _bridge_ctx() -> context_bridge.PlatformBridgeContext:
    return context_bridge.PlatformBridgeContext(fastmcp=app.fastmcp)


def _patch_ask(monkeypatch, answer=None, *, raises=None, reply_to=None, params=None):
    """Record the ask call and answer ``answer`` with ``reply_to`` and ``params`` (or raise ``raises``)."""
    calls: list[dict] = []

    async def fake_ask(question, *, answer_format, schema=None, **kwargs):
        calls.append({"question": question, "answer_format": answer_format, "schema": schema})
        if raises is not None:
            raise raises
        return AskAnswer(answer=answer, reply_to=reply_to, params=params)

    monkeypatch.setattr("tai42_skeleton.interactions.helper.ask", fake_ask)
    return calls


# -- seam (a): ElicitBridgeContext -------------------------------------------


def test_seam_a_derives_schema_and_maps_to_accepted(monkeypatch):
    # Scalar response_type -> parse_elicit_response_type wraps it as a {value}
    # form schema; the validated answer maps back to AcceptedElicitation.data.
    calls = _patch_ask(monkeypatch, answer={"value": 7})
    ctx = _bridge_ctx()

    result = asyncio.run(ctx.elicit("Pick a number", int))

    assert isinstance(result, AcceptedElicitation)
    assert result.data == 7
    assert calls[0]["answer_format"] == "form"
    # Schema fidelity: the derived form schema carries the wrapped scalar field.
    assert "value" in calls[0]["schema"]["properties"]


class _Profile(BaseModel):
    name: str
    age: int


def test_seam_a_model_response_type_round_trips(monkeypatch):
    calls = _patch_ask(monkeypatch, answer={"name": "Ada", "age": 36})
    ctx = _bridge_ctx()

    result = asyncio.run(ctx.elicit("Your profile", _Profile))

    assert isinstance(result, AcceptedElicitation)
    assert result.data == _Profile(name="Ada", age=36)
    # The model's own object schema is carried as the form answer-schema.
    assert set(calls[0]["schema"]["properties"]) == {"name", "age"}


def test_seam_a_timeout_raises_no_decline_round_trip(monkeypatch):
    _patch_ask(monkeypatch, raises=InteractionTimeoutError("no answer"))
    ctx = _bridge_ctx()

    with pytest.raises(InteractionTimeoutError):
        asyncio.run(ctx.elicit("Pick a number", int))


_QUOTE = ReplyTo(message_id="M-1", status=ReplyToStatus.FOUND, text="the earlier message")


def test_an_elicit_answered_by_a_quoting_reply_carries_the_quote_in_meta(monkeypatch):
    _patch_ask(monkeypatch, answer={"value": 7}, reply_to=_QUOTE, params={"reply_id": "r-1"})

    result = asyncio.run(_bridge_ctx().elicit("Pick a number", int))

    assert isinstance(result, BridgedAcceptedElicitation)
    assert result.action == "accept"
    assert result.data == 7
    assert result.meta == {ELICIT_META_REPLY_TO: _QUOTE.model_dump(mode="json")}


def test_an_elicit_answered_quoting_nothing_carries_a_null_quote(monkeypatch):
    _patch_ask(monkeypatch, answer={"value": 7})

    result = asyncio.run(_bridge_ctx().elicit("Pick a number", int))

    assert isinstance(result, BridgedAcceptedElicitation)
    assert result.data == 7
    assert result.meta == {ELICIT_META_REPLY_TO: None}


def test_the_bridged_result_dumps_its_meta_under_the_mcp_meta_key(monkeypatch):
    _patch_ask(monkeypatch, answer={"value": 7}, reply_to=_QUOTE)

    dumped = asyncio.run(_bridge_ctx().elicit("Pick a number", int)).model_dump(mode="json", by_alias=True)

    assert dumped == {
        "action": "accept",
        "data": 7,
        "_meta": {ELICIT_META_REPLY_TO: {"message_id": "M-1", "status": "found", "text": "the earlier message"}},
    }


# -- elicit_bridge_context: an active client context wins --------------------


def test_bridge_context_noop_when_a_context_is_already_active():
    # An elicit-capable client already established a context; the bridge must not
    # override it (that client answers in-client).
    real = Context(fastmcp=app.fastmcp)

    async def go() -> bool:
        from fastmcp.server.dependencies import get_context

        with set_context(real), context_bridge.bridge_context(app.fastmcp):
            return get_context() is real

    assert asyncio.run(go()) is True


def test_bridge_context_pushes_bridge_when_none_active():
    async def go() -> bool:
        with context_bridge.bridge_context(app.fastmcp):
            from fastmcp.server.dependencies import get_context

            return isinstance(get_context(), context_bridge.PlatformBridgeContext)

    assert asyncio.run(go()) is True


# -- integration: run_tool drives ctx.elicit through ask ----------------


def test_run_tool_routes_ctx_elicit_through_ask(monkeypatch):
    _patch_ask(monkeypatch, answer={"value": 42})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def needs_input(ctx: Context) -> int:
                """A tool that elicits a number from the human."""
                answer = await ctx.elicit("How many?", int)
                assert isinstance(answer, AcceptedElicitation)
                return answer.data

            # run_tool is the in-process caller path: no client, so ctx.elicit
            # resolves via the ask bridge (seam a).
            assert await app.tools.run_tool("needs_input", {}) == 42

    asyncio.run(run())
