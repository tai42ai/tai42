"""Door-contract probe tools: the async caller-ask a conversation route drives through its door
contract, an extras-echo tool that reveals the run ``extras`` a route's ``extras_expr`` built, and
a probe that calls an in-process conversation door tool from inside a run so a keyless fire proves
the door's unauthenticated refusal.

Each registers at import via ``@tai42_app.tools.tool`` and exposes an in-process observable (a
return value or an ``e2e_record`` Redis side effect), never a mock."""

from __future__ import annotations

import json
import os

from tai42_contract.app import tai42_app

from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings
from tai42_e2e_fixtures.tools.driving import driving_as


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_caller_ask(question: str, expiry_seconds: float) -> object:
    """Async-ask the run's CALLER, parking the run on a ``to="caller"`` interaction.

    Addresses the CALLER (not the user), so a conversation route classifies the park as an
    ``asks`` outcome surfaced through ``reply_expr`` and resumed through ``resume_expr``, rather
    than an out-of-band user park. Inherits the turn's bound execution identity (the route's
    execution key) when one is bound, binding a synthetic stand-in only on an auth-off stack that
    binds none — the same inherit-or-bind rule the user-ask probe uses, since the stored identity
    is what the resume rebinds as.

    RPUSHes ``{pid}`` onto ``e2e:rec:caller_ask:{interaction_id}`` so a spec reads that the ask ran
    EXACTLY ONCE (a resume substitutes the answer, never re-runs this tool). Returns the
    ``SuspendedInteraction`` the tool seam stamps into the park marker.
    """
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.authz.execution_identity import (
        get_execution_identity,
        reset_execution_identity,
        set_execution_identity,
    )
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask

    identity_token = (
        None
        if get_execution_identity() is not None
        else set_execution_identity(CallerIdentity(user_id="e2e-door-driver", execution_key_fingerprint="e2e-door-fp"))
    )
    try:
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        suspended = await ask(question, mode="async", expiry_at=expiry_at, to="caller")
    finally:
        if identity_token is not None:
            reset_execution_identity(identity_token)
    record = json.dumps({"pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:caller_ask:{suspended.interaction_id}", record))
    return suspended


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_schedule_park(question: str, expiry_seconds: float, record_key: str) -> object:
    """Async-ask (parking the run), recording the execution identity this dispatch is bound under.

    Binds NO identity of its own — it relies on the identity the caller bound, so on a schedule fire
    the recorded identity is the schedule's ``execution_key`` (proof the key bound in the worker) and
    the ask parks under it. A plain tool that async-parks is its OWN resuming driver (the platform
    binds a resume continuation only for an agent run), so it binds ``e2e_schedule_park_deliver`` as
    the stored continuation before it asks. RPUSHes ``{identity, interaction_id, pid}`` onto
    ``e2e:rec:{record_key}`` so a spec reads that the fire's run reached the park and under which
    identity. Returns the ``SuspendedInteraction`` the tool seam stamps into the park marker.
    """
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.authz.execution_identity import get_execution_identity
    from tai42_skeleton.interactions import ask

    bound = get_execution_identity()
    identity = bound.user_id if bound is not None else None
    with driving_as(continuation="e2e_schedule_park_deliver"):
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        suspended = await ask(question, mode="async", expiry_at=expiry_at)
    record = json.dumps({"identity": identity, "interaction_id": suspended.interaction_id, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{record_key}", record))
    return suspended


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_schedule_park_deliver(interaction_id: str, answer: object) -> dict:
    """The stored resume continuation an ``e2e_schedule_park`` park is resumed through.

    The platform's continuation seam fires it with ``{interaction_id, answer}`` under the park's
    stored (schedule execution-key) identity when the park resolves by an answer or its expiry. A
    schedule fire is receiver-less, so returning the answer as the run's terminal lets the platform
    subject-track it — no door completion to fire. Returns ``{answer}`` so a spec that drives the
    resume reads the value the continuation produced.
    """
    return {"answer": answer}


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_caller_hold(question: str, expiry_seconds: float) -> object:
    """Async-ask the run's CALLER, parking a ``to="caller"`` interaction that waits standalone.

    Unlike ``e2e_caller_ask`` (which rides an agent run's own park machinery), this is a plain tool
    dispatched directly on a subject door, so it is its OWN resuming driver: it binds
    ``e2e_caller_hold_deliver`` as the stored resume continuation before it asks. A later door whose
    ``resume_expr`` names the parked ``to="caller"`` id resumes it, and — the resuming door being
    receiver-less — the continuation's return subject-tracks as a waiting outcome a later run takes.
    Returns the ``SuspendedInteraction`` the tool seam stamps into the park marker.
    """
    from datetime import UTC, datetime, timedelta

    from tai42_skeleton.authz.execution_identity import get_execution_identity
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask

    identity = (
        None
        if get_execution_identity() is not None
        else CallerIdentity(user_id="e2e-hold-driver", execution_key_fingerprint="e2e-hold-fp")
    )
    with driving_as(continuation="e2e_caller_hold_deliver", identity=identity):
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        suspended = await ask(question, mode="async", expiry_at=expiry_at, to="caller")
    return suspended


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_caller_hold_deliver(interaction_id: str, answer: object) -> dict:
    """The stored resume continuation an ``e2e_caller_hold`` caller ask is resumed through.

    Fired once with ``{interaction_id, answer}`` under the park's stored identity when a door resumes
    the ask. Returns ``{answer}`` as the run's terminal so a receiver-less resuming door subject-tracks
    it as the waiting outcome a later run takes.
    """
    return {"answer": answer}


@tai42_app.tools.tool(tags={"e2e"}, extras_keys=frozenset({"tag"}))
async def e2e_extras_probe(marker: str) -> dict:
    """Return the run ``extras`` this dispatch was handed, keyed beside ``marker``.

    A door's ``extras_expr`` builds the extras mapping it hands the started run; the tool reads them
    off the run's ambient extras binding, so a spec proves the ``extras_expr`` result reached the
    run. ``marker`` is echoed back so a door can also assert its ``start_expr`` mapping. The result
    is ALSO RPUSHed onto ``e2e:rec:extras:{marker}`` so a receiver-less door (a hook, a schedule)
    that discards the return is still observable.
    """
    from collections.abc import Awaitable
    from typing import cast

    from tai42_contract.tools import current_extras
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    result = {"marker": marker, "extras": dict(current_extras())}
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:extras:{marker}", json.dumps(result)))
    return result


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_call_conversation_door(door: str, route_name: str, record_key: str) -> dict:
    """Call an in-process conversation door tool from inside THIS run, recording the outcome.

    ``door`` names the door tool to invoke — ``"message"`` calls ``send_conversation_message`` with a
    minimal valid body, ``"event"`` calls ``send_conversation_event`` with a minimal valid event. The
    door refuses a run with no accountable execution identity under an enabled gate BEFORE it resolves
    ``route_name``, so a keyless fire (a schedule with no ``execution_key``) reaches that refusal
    without the route needing to exist.

    RPUSHes ``{door, error, pid}`` onto ``e2e:rec:{record_key}`` recording the exception type the door
    raised (the worker's failure record a keyless fire produces), then RE-RAISES so the fire fails
    loudly and the refusal is never a silent drop. A door that returns instead of refusing records the
    unexpected success and raises, so a leak can never read as a pass.
    """
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    if door == "message":
        tool_name = "send_conversation_message"
        arguments: dict[str, object] = {"route_name": route_name, "external_user_id": "e2e-door-user", "text": "hi"}
    elif door == "event":
        tool_name = "send_conversation_event"
        arguments = {
            "route_name": route_name,
            "event": {"event_id": "e2e-door-evt", "kind": "e2e.probe", "payload": {}},
            "thread_id": "e2e-door-thread",
        }
    else:
        raise ValueError(f"e2e_call_conversation_door: unknown door {door!r} (expected 'message' or 'event')")

    async def _record(error: str | None) -> None:
        entry = json.dumps({"door": door, "error": error, "pid": os.getpid()})
        async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
            await cast(Awaitable[int], client.rpush(f"e2e:rec:{record_key}", entry))

    try:
        await tai42_app.tools.run_tool(tool_name, arguments)
    except Exception as exc:
        # Record the refusal type the door raised, then re-raise it loudly — never a silent drop.
        await _record(type(exc).__name__)
        raise
    await _record(None)
    raise RuntimeError(f"{tool_name} returned under a keyless fire instead of refusing the unauthenticated run")
