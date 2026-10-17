"""Reacting-form probe tools: a driver that parks a reacting form ask over a delivery channel,
and the reaction handler that form runs while it is open. Each registers at import via
``@tai42_app.tools.tool`` and exposes an ``e2e_record`` Redis observable or its return value."""

from __future__ import annotations

import json
import os
from typing import Any

from tai42_contract.app import tai42_app

from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings
from tai42_e2e_fixtures.tools.driving import driving_as

# The prefix the handler writes in front of the picked choice when it re-fills ``label``, so a
# leg reads the reaction's own update back out of the channel's answer.
FORM_REACTION_LABEL_PREFIX = "picked "


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_reacting_form_ask(
    question: str,
    channel: str,
    recipient: str,
    schema: dict[str, Any],
    reactions: dict[str, Any],
    expiry_seconds: float,
) -> dict:
    """Park an async form ask over ``channel`` whose open form runs ``e2e_form_reaction``.

    Binds ``e2e_async_resume`` as the resume continuation and drives under the caller's own
    execution identity (the door binds it from the calling key), so the reaction handler and
    the resume both run as that key. Returns the parked ``interaction_id``; the answer fires
    ``e2e_async_resume``, which records it under ``async_resume:{interaction_id}``.
    """
    from datetime import UTC, datetime, timedelta

    from tai42_skeleton.interactions import ask

    with driving_as(continuation="e2e_async_resume"):
        suspended = await ask(
            question,
            answer_format="form",
            schema=schema,
            channel=channel,
            recipient=recipient,
            reaction_tool="e2e_form_reaction",
            reactions=reactions,
            mode="async",
            expiry_at=datetime.now(UTC) + timedelta(seconds=expiry_seconds),
        )
    return {"interaction_id": suspended.interaction_id}


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_form_reaction(interaction_id: str, event: dict[str, Any], values: dict[str, Any]) -> dict:
    """The reaction handler of a form parked by ``e2e_reacting_form_ask``.

    RPUSHes ``{"event", "values", "pid"}`` onto ``e2e:rec:form_reaction:{interaction_id}`` so a
    leg reads which event fired with which values filled so far, then answers a ``field_changed``
    of ``tier`` by re-filling ``label`` with :data:`FORM_REACTION_LABEL_PREFIX` and the picked
    tier; every other event gets an empty update.
    """
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    record = json.dumps({"event": event, "values": values, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:form_reaction:{interaction_id}", record))
    if event.get("kind") == "field_changed" and event.get("field") == "tier":
        return {"values": {"label": f"{FORM_REACTION_LABEL_PREFIX}{values.get('tier', '')}"}}
    return {}
