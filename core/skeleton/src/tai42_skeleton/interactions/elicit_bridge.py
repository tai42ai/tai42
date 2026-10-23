"""Bridge FastMCP elicitation onto the interactions ``ask`` channel.

A tool's ``ctx.elicit()`` normally resolves only when the calling client
supports elicitation; an in-process caller (agent, webhook-triggered run,
scheduled backend) has no client to answer, so the call dead-ends. This bridge
routes such an elicit through the SAME human-in-the-loop waiter/store as
``ask`` (the interactions system), so a human answers on any interactions
channel and the typed result flows back to the caller.

The skeleton's in-process Context-injection layer (the ``run_tool`` path) drives
this: the in-process bridge context (see
:mod:`tai42_skeleton.tools.context_bridge`) routes ``ctx.elicit`` through
:func:`resolve_elicit`, which DERIVES the schema from the ``response_type`` (a
Python type) via ``parse_elicit_response_type`` and carries it as the form
answer-schema.

Accept-or-raise: a validated form answer maps to a :class:`BridgedAcceptedElicitation` (an
``AcceptedElicitation`` that also carries the MCP result's ``_meta`` slot, holding the message the
answering reply quoted under :data:`~tai42_contract.interactions.ELICIT_META_REPLY_TO`, which
is ``None`` because the elicitation is asked with no channel and answered in the inbox); a
timeout / no-answer RAISES (the no-answerable-path invariant). The bridge never
rounds a decline or cancel through ``ask`` — an unanswerable elicit is a
loud error, never a silent default.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.elicitation import (
    AcceptedElicitation,
    handle_elicit_accept,
    parse_elicit_response_type,
)
from pydantic import Field
from tai42_contract.interactions import ELICIT_META_REPLY_TO, AskAnswer

from tai42_skeleton.interactions import helper

logger = logging.getLogger(__name__)


class BridgedAcceptedElicitation[T](AcceptedElicitation[T]):
    """An accepted elicitation answered through ``ask``, with the MCP result's ``_meta`` slot.

    ``meta`` is the same field, name and alias as an MCP result's ``meta``: a tool reads
    ``result.meta`` and ``model_dump(by_alias=True)`` writes ``_meta``. It holds the message the
    answering reply quoted under :data:`~tai42_contract.interactions.ELICIT_META_REPLY_TO`, as
    ``ReplyTo`` JSON, or ``None`` when the reply quoted none or the answer did not come over a
    channel; an elicitation is answered in the inbox, so it reads ``None`` there. ``action`` and
    ``data`` are the accepted result's own.
    """

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


async def answer_elicit_via_ask(message: str, schema: dict[str, Any]) -> AskAnswer:
    """Ask a human ``message`` with ``schema`` as the form answer-schema and return the ``AskAnswer``.

    Schema fidelity is the point: the schema is carried through intact so the caller gets exactly the shape
    it asked for. A timeout / no-answer raises out of ``ask`` (accept-or-raise); nothing is swallowed.
    """
    answered = await helper.ask(message, answer_format="form", schema=schema)
    if not isinstance(answered, AskAnswer):
        raise TypeError(f"a sync ask answered with {type(answered).__name__}, not an AskAnswer")
    return answered


async def resolve_elicit(
    message: str,
    response_type: Any = None,
    *,
    response_title: str | None = None,
    response_description: str | None = None,
) -> BridgedAcceptedElicitation[Any]:
    """Derive the elicit schema from a Python ``response_type`` and ask a human through ``ask``.

    Maps the validated answer back to the typed accepted result, with the quote the answering
    reply carried in its ``_meta``; the answer's ``params`` are not carried. No decline/cancel
    round-trip — a no-answer raises.
    """
    config = parse_elicit_response_type(
        response_type,
        response_title=response_title,
        response_description=response_description,
    )
    answered = await answer_elicit_via_ask(message, config.schema)
    accepted = handle_elicit_accept(config, answered.answer)
    reply_to = None if answered.reply_to is None else answered.reply_to.model_dump(mode="json")
    return BridgedAcceptedElicitation[Any](data=accepted.data, _meta={ELICIT_META_REPLY_TO: reply_to})
