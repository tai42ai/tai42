"""The delivery tool that closes a CHAINED park: a nested run's terminal, back into the loop.

An agent that dispatches a tool binds a chained completion around the call
(:func:`~tai42_agents._internal.nested_dispatch.nested_tool_dispatch`). When the tool drives a
nested run that async-parks, the agent does not adopt that park — it parks on the CALL, under
the chained key that binding carries — and the nested run keeps its own park and its own
resume. This module is the other end of that wire: the tool the nested run's driver fires when
it finally reaches a terminal, which reverses the chained key through the SAME super-step
barrier every answered park resumes through and re-enters the agent loop with that terminal as
the tool's result.

The whole chain, for a flow called inside an agent turn::

    the human answers -> the driver's resume -> the nested run's terminal -> deliver_chained_park
                      -> agent_resume -> the agent loop -> the agent's own completion

Nothing here knows what a flow is. It reads the contract-generic completion payload — the bound
context merged with ``{result, completion_id, status}`` — so ANY driver that fires completions
that way chains, and the agents plugin names none of them.

Three statuses arrive, and each has one job:

* :data:`~tai42_contract.interactions.PARK_COMPLETION_SUCCEEDED` — the terminal's ``result``
  becomes the tool's result and the agent runs on;
* :data:`~tai42_contract.interactions.PARK_COMPLETION_REPARKED` — not a terminal at all: the
  nested run asked again, so the chained park's INHERITED horizon is extended to the new
  deadline and nothing is resumed;
* everything else — the explicit ``FAILED``, an unstamped fire, an unrecognized value — is a
  non-success terminal, resumed as a model-visible tool ERROR, so the model reads that the call
  came back empty and answers around it instead of the turn stalling on silence.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Final

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_REPARKED,
    PARK_COMPLETION_SUCCEEDED,
)

from tai42_agents._internal.park.driver import agent_resume, chained_park_horizon
from tai42_agents._internal.park.index import extend_park_horizon, is_resolved_tombstone, read_park_entry
from tai42_agents._internal.park.middleware import park_error_answer

logger = logging.getLogger(__name__)

# Registered name of the hidden tool a nested run's driver fires with its terminal. Bound as the
# COMPLETION continuation of every chained dispatch, so the nested driver addresses it without
# naming this plugin. Must equal the bound tool name.
CHAINED_PARK_DELIVERY_TOOL_NAME: Final[str] = "deliver_chained_park"


def _terminal_succeeded(chain_token: str, completion_id: str | None, status: str | None) -> bool:
    """Whether a terminal fire's ``status`` names the clean-success terminal — and the one place
    a non-success fire is ANNOUNCED.

    The shared contract vocabulary carries exactly one success value. Every other shape is
    non-success — an UNSTAMPED fire (``None``: a driver that predates the status field, or one
    that omits it) and an UNRECOGNIZED value alike — because resuming an unknown terminal as if
    it were the answer would hand the model a failure payload dressed as a result.

    Those two shapes are a driver/delivery version skew, and the skew is otherwise INVISIBLE:
    the fire still resumes the run, so a whole fleet can silently degrade every successful
    outcome into an error. This warning is the only detection, so EVERY non-success fire — the
    explicit failure included — names the chained call and WHICH shape arrived."""
    if status == PARK_COMPLETION_SUCCEEDED:
        return True
    if status is None:
        reason = "it carried NO status (an unstamped fire — a driver that predates the status field)"
    elif status == PARK_COMPLETION_FAILED:
        # Phrased about the VALUE, not the sender: a caller whose own default supplies this
        # constant reaches here without having reported anything.
        reason = f"its status is the non-success terminal {PARK_COMPLETION_FAILED!r}"
    else:
        reason = f"it carried the unrecognized status {status!r} (not in the contract vocabulary)"
    logger.warning(
        "agents: %s (%s, completion %s) resumes the waiting run with a tool error instead of a result because %s",
        CHAINED_PARK_DELIVERY_TOOL_NAME,
        chain_token,
        completion_id,
        reason,
    )
    return False


async def _extend_horizon(chain_token: str, expiry_at: str | None) -> dict[str, Any]:
    """Move a chained park's inherited horizon out to the run's NEW deadline.

    The park entry is re-clamped through the SAME rule the persist used
    (:func:`~tai42_agents._internal.park.driver.chained_park_horizon`), against the retention
    bound the entry recorded — so an extension can never carry a park past the window its own
    state survives, and never past the configured cap.

    A key with no live entry is a no-op, not an error, and that is the COMMON case rather than a
    fault: the first park of a chained call always notifies before the waiting run has finished
    recording its own park (the nested run parks first, by construction); a park the caller
    ADOPTED as its own — an ``ask_user`` raised directly by a chained dispatch — notifies a chain
    nothing ever parks on; and a resolved or detached chain has nothing left to extend."""
    entry = await read_park_entry(chain_token)
    if entry is None or is_resolved_tombstone(entry):
        logger.debug("agents: re-park notice for chained call %s has no live park to extend", chain_token)
        return {"status": "not_parked"}
    retention = entry.get("retention_bound")
    horizon = chained_park_horizon(expiry_at, datetime.fromisoformat(retention) if retention else None)
    extended = await extend_park_horizon(
        chain_token, entry["thread_id"], entry["superstep_id"], datetime.fromisoformat(horizon)
    )
    return {"status": "extended" if extended else "unchanged", "expiry_at": horizon}


async def deliver_chained_park(
    chain_token: str | None = None,
    result: Any = None,
    completion_id: str | None = None,
    status: str | None = PARK_COMPLETION_FAILED,
    expiry_at: str | None = None,
    chained_context: dict[str, Any] | None = None,
    delivery_thread_id: str | None = None,
) -> Any:
    """Deliver a nested run's terminal to the agent run that parked on the CALL.

    The completion continuation an agent binds around every tool dispatch it can wait on: when
    the tool drove a nested run that async-parked, the agent parked on ``chain_token`` and this
    is what resumes it. The fire is the contract-generic completion payload — the bound chained
    context merged with ``{result, completion_id, status}`` — so any driver that fires
    completions reaches it without naming this plugin.

    ``status`` decides the outcome: ``succeeded`` resumes the waiting run with ``result`` as the
    awaited tool's result; ``reparked`` is not a terminal at all — the nested run asked again,
    so the chained park's inherited horizon is extended to ``expiry_at`` and nothing is resumed;
    every other shape (the explicit ``failed``, an unstamped fire, an unrecognized value) is a
    non-success terminal, resumed as a model-visible tool ERROR naming what arrived. An OMITTED
    ``status`` keeps this tool's published fail-safe default, so it is reported as that terminal
    rather than as an unstamped fire. A ``succeeded`` fire's ``result`` is passed through
    exactly as the nested run produced it, empty included — the model reads what the call
    returned, not an interpretation of it.

    Delivery is at-least-once, and the resume is idempotent for it: the super-step barrier
    buffers an answer once (a redelivery is a no-op) and a resolved super-step leaves a
    tombstone a late fire lands on benignly. ``completion_id`` is the driver's stable id for the
    resolved terminal, carried for correlation in the logs — the barrier, not this id, is what
    makes redelivery safe.

    Returns the resumed run's own outcome (its terminal value, a ``buffered`` receipt while
    sibling parks of the same super-step are outstanding, a ``suspended`` receipt if the resumed
    run parked again, or ``already_resolved``), so the driver that fired sees what its delivery
    led to.

    A fire with NO ``chain_token`` is unroutable and no retry can ever land it, so it is dropped
    LOUDLY rather than raising into an endless redelivery. A token whose park entry is ABSENT
    raises instead: an entry can be missing because the waiting run has not finished recording
    its park yet, which the platform's redelivery fixes, so the retry ticket is kept. A run that
    ends without ever parking on a chain it claimed detaches that key itself, which is what
    turns the truly-dead case into the benign tombstone rather than an endless retry.

    ``chained_context`` and ``delivery_thread_id`` are the rest of the chained binding, accepted
    because the whole context rides every fire: the embedded caller binding (which this tool
    never needs — the waiting run's own park entry carries its delivery address) and the
    reserved thread field the platform's park-by-thread index reads off the context. Neither is
    read here; they are declared so a complete fire is never a signature error."""
    if not chain_token:
        logger.error(
            "agents: a chained park completion (%s) fired with no chain_token; it names no waiting run, so the "
            "terminal cannot be delivered and is dropped",
            completion_id,
        )
        return {"status": "dropped"}
    if status == PARK_COMPLETION_REPARKED:
        return await _extend_horizon(chain_token, expiry_at)
    if _terminal_succeeded(chain_token, completion_id, status):
        answer: Any = result
    else:
        answer = park_error_answer(
            f"the call this run is waiting on ended without a result (terminal status {status!r}); "
            "nothing was delivered, so continue without it"
        )
    return await agent_resume(chain_token, answer)


def register_chained_park_tool() -> None:
    """Idempotently bind the hidden ``deliver_chained_park`` completion tool.

    Called from the registration of every agent whose dispatches can chain, mirroring
    :func:`~tai42_agents._internal.park.resume_tool.register_agent_resume_tool`: the first call
    binds, a later one on the same epoch catches the FastMCP duplicate-bind error and no-ops, so
    a box loading any subset of those agents — and every reload epoch — ends with exactly one
    binding. Any OTHER error propagates loudly.

    Registered ``force=True`` (a mandatory mechanism, never an operator-excludable catalog tool)
    and ``tai42/hidden`` (never offered to a model as a callable tool)."""
    try:
        tai42_app.tools.tool(
            name=CHAINED_PARK_DELIVERY_TOOL_NAME,
            tags={"agents"},
            meta={"tai42/hidden": True},
            force=True,
        )(deliver_chained_park)
    except ValueError as exc:
        if "already exists" not in str(exc):
            raise
        logger.debug("deliver_chained_park tool already bound this epoch; registration is a no-op")
