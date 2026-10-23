"""Quoted-message probes: a turn tool that records what it was started with and replies, a sync
``ask`` that records the whole answer it got back, and an elicitation that records its typed result
with the message the answering reply quoted. Each registers at import via ``@tai42_app.tools.tool``
and RPUSHes its observation onto an ``e2e:rec:{key}`` Redis list the harness reads."""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel
from tai42_contract.app import tai42_app
from tai42_contract.interactions import ELICIT_META_REPLY_TO

from tai42_e2e_fixtures.tools.ask_results import answered
from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings


async def _rpush(key: str, entry: dict[str, Any]) -> None:
    """RPUSH ``entry`` (with this process's pid) as JSON onto ``e2e:rec:{key}``."""
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    record = json.dumps({**entry, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{key}", record))


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_record_and_reply(key: str, value: str, reply: str) -> str:
    """Record ``{"value", "pid"}`` under ``e2e:rec:{key}`` and return ``reply``.

    A conversation tool target whose ``start_expr`` maps the turn's payload into ``value`` and
    its reply text into ``reply``: the turn both leaves its observation for the harness and
    answers the participant, so a later message can quote that answer."""
    await _rpush(key, {"value": value})
    return reply


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_ask_recorded(
    key: str,
    question: str,
    channel: str,
    recipient: str | None = None,
    answer_format: str = "text",
    options: list[str] | None = None,
) -> object:
    """Ask ``question`` over ``channel`` (to ``recipient``, else the channel's default), blocking
    until it is answered, and record the whole answer object.

    RPUSHes ``{"answer", "reply_to", "params", "pid"}`` onto ``e2e:rec:{key}``: the typed answer,
    the message the answering reply quoted as ``ReplyTo`` JSON (``None`` when it quoted none) and the
    answer's params. Returns the answer."""
    answer = answered(
        await tai42_app.interactions.ask(
            question, channel=channel, recipient=recipient, answer_format=answer_format, options=options
        )
    )
    reply_to = None if answer.reply_to is None else answer.reply_to.model_dump(mode="json")
    await _rpush(key, {"answer": answer.answer, "reply_to": reply_to, "params": answer.params})
    return answer.answer


class _ElicitedPick(BaseModel):
    """The typed response the elicitation probe asks for."""

    colour: str
    count: int


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_elicit_record(key: str, question: str) -> str:
    """Elicit an ``_ElicitedPick`` with ``question`` and record the accepted result.

    Run in process (a conversation tool turn), ``ctx.elicit`` is answered through the interactions
    ``ask`` channel. RPUSHes ``{"data", "typed", "reply_to", "pid"}`` onto ``e2e:rec:{key}``: the
    accepted data, whether it arrived as an ``_ElicitedPick``, and the result's
    ``meta["tai42/reply_to"]``. Returns a short acknowledgement as the turn's reply."""
    from fastmcp.server.dependencies import get_context
    from fastmcp.server.elicitation import AcceptedElicitation

    result = await get_context().elicit(question, _ElicitedPick)
    if not isinstance(result, AcceptedElicitation):
        raise TypeError(f"the elicitation ended with {type(result).__name__}, not an accepted result")
    meta = getattr(result, "meta", None)
    if not isinstance(meta, dict):
        raise TypeError(f"the accepted elicitation carries no meta mapping, got {meta!r}")
    data = result.data
    await _rpush(
        key,
        {
            "data": data.model_dump(mode="json") if isinstance(data, BaseModel) else data,
            "typed": isinstance(data, _ElicitedPick),
            "reply_to": meta.get(ELICIT_META_REPLY_TO),
        },
    )
    return "elicited"
