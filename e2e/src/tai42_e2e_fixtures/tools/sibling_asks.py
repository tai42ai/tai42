"""A conversation tool-target whose one run asks its user two questions at once, and the stored
resume continuation that finishes the run only when both are answered. Registers at import via
``@tai42_app.tools.tool``.

``e2e_sibling_asks_park`` parks two async asks inside one dispatch, so both are members of one
run: both to the same channel and recipient, or both to the caller. ``e2e_sibling_asks_resume`` is the continuation each
answer fires: the first answer it sees returns a :class:`ResumeBuffered` naming the other ask (the
run stays parked), the second holds the run open for ``hold_seconds`` and returns the run's
terminal. A spec reads what each fire did from the probe channel."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from tai42_contract.app import tai42_app
from tai42_contract.tools import TOOL_META_PAUSES

from tai42_e2e_fixtures.tools.ask_results import parked, resumed_with
from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings
from tai42_e2e_fixtures.tools.driving import driving_as

# Far enough out that a driven answer always resolves both asks first.
_SIBLING_ASKS_EXPIRY_SECONDS = 3600.0

# The hash mapping each parked ask's id to ``{marker, slot, other, hold_seconds}``, so the
# continuation recovers its run from the interaction id alone.
_SIBLING_ASKS_ROUTE_KEY = "e2e:sibling_asks:route"

_SLOTS = ("first", "second")


def _answers_key(marker: str) -> str:
    return f"e2e:sibling_asks:answers:{marker}"


@tai42_app.tools.tool(tags={"e2e"}, meta={TOOL_META_PAUSES: True})
async def e2e_sibling_asks_park(
    marker: str,
    channel: str | None = None,
    recipient: str | None = None,
    hold_seconds: float = 0.0,
    to: Literal["user", "caller"] = "user",
) -> object:
    """Ask two questions in parallel and park the run on both.

    Runs as the target of a ``target_kind=tool`` conversation route. Both asks (questions
    ``"<marker>:first"`` and ``"<marker>:second"``) are one step's group ``"<marker>:asks"`` and
    go to ``to``: the user on ``channel`` and ``recipient``, or the caller, whose door resumes them
    from its ``$parked``. They park under the door's park-completion binding, with
    ``e2e_sibling_asks_resume`` bound as their resume continuation.
    Returns one ``SuspendedInteraction`` carrying both ids, so the turn ends silently and the run
    stays parked until both are answered. ``hold_seconds`` is how long the fire that completes the
    run holds it open before returning its terminal.

    RPUSHes ``{interaction_ids, pid}`` (first ask, then second) onto
    ``e2e:rec:sibling_asks_park:{marker}``: the barrier that the turn ran to the park."""
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_contract.interactions import get_park_completion
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.interactions import ask

    address = get_park_completion()
    if address.tool is None or address.thread_id is None:
        raise RuntimeError("e2e_sibling_asks_park ran with no park-completion binding from the conversation door")

    expiry_at = datetime.now(UTC) + timedelta(seconds=_SIBLING_ASKS_EXPIRY_SECONDS)
    sentinels = []
    with driving_as(continuation="e2e_sibling_asks_resume"):
        for slot in _SLOTS:
            sentinels.append(
                parked(
                    await ask(
                        f"{marker}:{slot}",
                        mode="async",
                        expiry_at=expiry_at,
                        channel=channel,
                        recipient=recipient,
                        to=to,
                        group_id=f"{marker}:asks",
                    )
                )
            )
    interaction_ids = [sentinel.interaction_id for sentinel in sentinels]

    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        for index, slot in enumerate(_SLOTS):
            route = {
                "marker": marker,
                "slot": slot,
                "other": interaction_ids[1 - index],
                "hold_seconds": hold_seconds,
            }
            await cast(Awaitable[int], client.hset(_SIBLING_ASKS_ROUTE_KEY, interaction_ids[index], json.dumps(route)))
        park_record = json.dumps({"interaction_ids": interaction_ids, "pid": os.getpid()})
        await cast(Awaitable[int], client.rpush(f"e2e:rec:sibling_asks_park:{marker}", park_record))
    return sentinels[0].model_copy(update={"interaction_ids": interaction_ids})


@tai42_app.tools.tool(tags={"e2e"}, meta={TOOL_META_PAUSES: True})
async def e2e_sibling_asks_resume(interaction_id: str, ask_result: Any) -> object:
    """The resume continuation each of the two asks fires.

    Stores the answer under its slot (HSETNX, so a redelivered fire stores it once) and RPUSHes
    ``{slot, interaction_id, buffered, pid}`` onto ``e2e:rec:sibling_asks_resume:{marker}``. While
    the other ask is unanswered it returns ``ResumeBuffered`` naming it; once both are answered it
    sleeps ``hold_seconds`` and returns ``{"result": {"answer": "<first>+<second>"}}``, the shape a
    route's ``reply_expr`` of ``.result.answer`` maps."""
    import asyncio
    from collections.abc import Awaitable
    from typing import cast

    from tai42_contract.interactions import ResumeBuffered
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    answer = resumed_with(ask_result).answer
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        raw_route = await cast("Awaitable[str | None]", client.hget(_SIBLING_ASKS_ROUTE_KEY, interaction_id))
        if raw_route is None:
            raise RuntimeError(f"sibling-asks resume for unrouted interaction {interaction_id!r}")
        route = json.loads(raw_route)
        answers_key = _answers_key(route["marker"])
        await cast(Awaitable[int], client.hsetnx(answers_key, route["slot"], json.dumps(answer)))
        answers = await cast("Awaitable[dict[str, str]]", client.hgetall(answers_key))
        buffered = len(answers) < len(_SLOTS)
        record = json.dumps(
            {"slot": route["slot"], "interaction_id": interaction_id, "buffered": buffered, "pid": os.getpid()}
        )
        await cast(Awaitable[int], client.rpush(f"e2e:rec:sibling_asks_resume:{route['marker']}", record))
    if buffered:
        return ResumeBuffered(remaining_ids=[route["other"]])
    await asyncio.sleep(route["hold_seconds"])  # noqa: TID251 — holds the run open while it owes its reply
    return {"result": {"answer": "+".join(str(json.loads(answers[slot])) for slot in _SLOTS)}}
