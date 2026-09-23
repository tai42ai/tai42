"""Probe tool targets for the overlap bridge suite.

Two conversation tool-targets the overlap specs drive:

* :func:`e2e_overlap_probe` records the exact turn payload it was dispatched with — the whole
  ``message`` text plus the ``messages`` batch and the ``superseded`` records a ``deliver="all"``
  turn carries — onto the harness probe channel, so a spec reads back what one turn actually
  carried. Given ``hold_seconds`` it also holds the turn open (an ``await asyncio.sleep``, so the
  cancel watcher can cancel it) — the running turn a newer message overlaps.
* :func:`e2e_overlap_yield` reads the pending seam
  (``tai42_app.conversations.pending_messages``) with the ambient turn's own lead id and, when a
  newer message is waiting, hands the turn over by raising
  :class:`~tai42_contract.conversations.TurnSupersededError` — the cooperative yield.

Both register at import via ``@tai42_app.tools.tool`` and expose their observation as an
``e2e_record`` Redis side effect (never a mock), so a spec reads what actually happened inside
the real turn.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable
from typing import cast

from tai42_contract.app import tai42_app
from tai42_contract.conversations import TurnSupersededError, current_conversation_turn
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings

# Hard ceiling on a probe hold, mirroring the other sleeping probes: a mis-scripted spec can
# never hang the turn engine on this tool.
_HOLD_SECONDS_MAX = 30.0


async def _rpush(key: str, entry: dict[str, object]) -> None:
    """RPUSH one JSON ``entry`` onto the harness probe list ``e2e:rec:{key}``."""
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast("Awaitable[int]", client.rpush(f"e2e:rec:{key}", json.dumps(entry)))


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_overlap_probe(
    key: str,
    message: str,
    messages: list | None = None,
    superseded: list | None = None,
    hold_seconds: float = 0.0,
) -> str:
    """Record the turn payload this tool target was dispatched with, then optionally hold the turn.

    Runs as a ``target_kind=tool`` conversation route whose ``start_expr`` maps the turn's
    ``message`` / ``messages`` / ``superseded`` keys onto these kwargs. It RPUSHes
    ``{message, messages, superseded, pid}`` onto ``e2e:rec:{key}`` — one entry per turn, in turn
    order — so a spec reads back the whole text, the ordered ``deliver="all"`` batch, and the
    superseded records each turn actually carried. The push is the turn-entered barrier a spec
    waits on before posting the messages that overlap this turn.

    ``hold_seconds`` (bounded at 30) then holds the turn open with an ``await asyncio.sleep`` — an
    async body runs inline on the event loop, so the overlap cancel watcher can cancel it, and a
    held turn is the running turn a newer message overlaps. Returns the whole ``message`` text as
    the reply (a route with no ``reply_expr`` passes it straight back)."""
    await _rpush(key, {"message": message, "messages": messages, "superseded": superseded, "pid": os.getpid()})
    if hold_seconds > 0:
        await asyncio.sleep(min(hold_seconds, _HOLD_SECONDS_MAX))  # noqa: TID251 — the held turn under test, bounded above
    return message


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_overlap_yield(
    key: str,
    message: str,
    messages: list | None = None,
    superseded: list | None = None,
    wait_seconds: float = 8.0,
) -> str:
    """Yield the turn to a newer message when the pending seam reports one is waiting.

    Runs as a ``target_kind=tool`` conversation route. It reads the ambient
    :class:`~tai42_contract.conversations.ConversationTurnRef` the platform deposits around the
    turn, RPUSHes an ``entered`` barrier onto ``e2e:rec:{key}:entered`` (so a spec posts the
    overlapping message only once this turn is genuinely running and has already gathered), then
    polls ``tai42_app.conversations.pending_messages(thread_id, after=lead_id)`` until a newer
    participant message is pending or ``wait_seconds`` (bounded at 30) elapses.

    It RPUSHes ``{message, messages, superseded, pending, pid}`` onto ``e2e:rec:{key}`` — the
    turn's whole text, its ``deliver="all"`` batch/superseded, and the pending message ids it
    observed — then, when anything is pending, hands the turn over by raising
    :class:`~tai42_contract.conversations.TurnSupersededError` with the newest pending id as the
    successor. With nothing pending it returns the whole ``message`` text as an ordinary reply."""
    turn = current_conversation_turn()
    if turn is None:
        raise RuntimeError("e2e_overlap_yield ran outside a conversation turn; no ambient turn ref was deposited")
    await _rpush(f"{key}:entered", {"lead": turn.message_id, "pid": os.getpid()})

    deadline = asyncio.get_running_loop().time() + min(wait_seconds, _HOLD_SECONDS_MAX)
    pending = await tai42_app.conversations.pending_messages(turn.thread_id, after=turn.message_id)
    while not pending and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)  # noqa: TID251 — bounded poll for a newer pending message
        pending = await tai42_app.conversations.pending_messages(turn.thread_id, after=turn.message_id)

    await _rpush(
        key,
        {
            "message": message,
            "messages": messages,
            "superseded": superseded,
            "pending": [p.message_id for p in pending],
            "pid": os.getpid(),
        },
    )
    if pending:
        raise TurnSupersededError(pending[-1].message_id)
    return message
