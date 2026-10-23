"""Reacting-form probe tools: a driver that parks a reacting form ask over a delivery channel,
and the reaction handler that form runs while it is open. Each registers at import via
``@tai42_app.tools.tool`` and exposes an ``e2e_record`` Redis observable or its return value."""

from __future__ import annotations

import json
import os
from typing import Any

from tai42_contract.app import tai42_app

from tai42_e2e_fixtures.tools.ask_results import parked
from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings
from tai42_e2e_fixtures.tools.driving import driving_as

# The prefix the handler writes in front of the picked choice when it re-fills ``label``, so a
# leg reads the reaction's own update back out of the channel's answer.
FORM_REACTION_LABEL_PREFIX = "picked "

# The tier whose pick makes the handler raise, so a leg reads a channel's could-not-update line.
FORM_REACTION_FAILING_TIER = "silver"

# The tier whose pick the handler answers with a field error on ``tier`` (this sentence), so a
# leg reads a channel's error line for a reaction's field errors.
FORM_REACTION_REFUSED_TIER = "bronze"
FORM_REACTION_REFUSED_SENTENCE = "The e2e reaction refuses this tier"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_reacting_form_ask(
    question: str,
    channel: str,
    recipient: str,
    schema: dict[str, Any],
    reactions: dict[str, Any],
    expiry_seconds: float,
    wording: dict[str, Any] | None = None,
) -> dict:
    """Park an async form ask over ``channel`` whose open form runs ``e2e_form_reaction``.

    Binds ``e2e_async_resume`` as the resume continuation and drives under the caller's own
    execution identity (the door binds it from the calling key), so the reaction handler and
    the resume both run as that key. ``wording`` is the ask's caller words, passed as they are.
    Returns the parked ``interaction_id``; the answer fires
    ``e2e_async_resume``, which records it under ``async_resume:{interaction_id}``.
    """
    from datetime import UTC, datetime, timedelta

    from tai42_skeleton.interactions import ask

    with driving_as(continuation="e2e_async_resume"):
        suspended = parked(
            await ask(
                question,
                answer_format="form",
                schema=schema,
                channel=channel,
                recipient=recipient,
                reaction_tool="e2e_form_reaction",
                reactions=reactions,
                wording=wording,
                mode="async",
                expiry_at=datetime.now(UTC) + timedelta(seconds=expiry_seconds),
            )
        )
    return {"interaction_id": suspended.interaction_id}


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_form_reaction(interaction_id: str, event: dict[str, Any], values: dict[str, Any]) -> dict:
    """The reaction handler of a form parked by ``e2e_reacting_form_ask``.

    RPUSHes ``{"event", "values", "pid"}`` onto ``e2e:rec:form_reaction:{interaction_id}`` so a
    leg reads which event fired with which values filled so far, then answers a ``field_changed``
    of ``tier`` by re-filling ``label`` with :data:`FORM_REACTION_LABEL_PREFIX` and the picked
    tier — except a pick of :data:`FORM_REACTION_FAILING_TIER`, which raises, and a pick of
    :data:`FORM_REACTION_REFUSED_TIER`, which answers :data:`FORM_REACTION_REFUSED_SENTENCE` as
    the field's error; every other event gets an empty update.
    """
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    record = json.dumps({"event": event, "values": values, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:form_reaction:{interaction_id}", record))
    if event.get("kind") == "field_changed" and event.get("field") == "tier":
        if values.get("tier") == FORM_REACTION_FAILING_TIER:
            raise RuntimeError(f"the e2e reaction refuses the {FORM_REACTION_FAILING_TIER} tier")
        if values.get("tier") == FORM_REACTION_REFUSED_TIER:
            return {"errors": {"tier": FORM_REACTION_REFUSED_SENTENCE}}
        return {"values": {"label": f"{FORM_REACTION_LABEL_PREFIX}{values.get('tier', '')}"}}
    return {}
