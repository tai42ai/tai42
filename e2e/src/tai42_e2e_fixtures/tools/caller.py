"""Caller-ask probe tools: a run that asks its CALLER and parks, the resume continuation
that re-drives the restored chain, and a relay that resumes a parked caller ask by subject.

``held_run`` reaches ``held_ask`` one frame down, which async-asks the caller through
``ask(to="caller", mode="async")`` — the ask parks on the run's subject and returns a
suspended receipt at once, and its ``asked_by`` is the chain above the ask frame (``held_run``,
or ``held_run`` then ``held_deeper`` on a deep re-park). On resume the stored continuation
``held_run_resume`` re-enters the parked run with the answer, restoring the run's call chain,
and either finalizes with the answer or asks the caller a SECOND time (a re-park); the second
ask's ``asked_by`` is the restored chain, so a spec reads it back off ``list_parked`` and sees
the continuation tool's own name is never in it. ``caller_relay`` resumes the single caller
ask on the current run's subject through the generic ``list_parked``/``resume_parked`` tools.
"""

from __future__ import annotations

import json
import os
from typing import Literal

from tai42_contract.app import tai42_app

from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings
from tai42_e2e_fixtures.tools.driving import driving_as

# The synthetic execution identity a caller-ask driver binds when the stack has bound none
# (the auth-off profiles), stored as the park's ``continuation_identity`` so a resume that
# rebound it is provable — a value nothing else on the stack produces.
_CALLER_PARK_IDENTITY = "e2e-caller-driver"

# The resume answer that makes a resumed ``held_run`` drive reach a FAILED terminal: the drive
# RETURNS a failure-stamped outcome (a business-error terminal, not a plain raise, which would
# mean "transient, redeliver"), so a receiverless resume leaves a ``failed`` waiting outcome.
_FAIL_ANSWER = "__held_fail__"

# The resume answer that makes the FIRST drive raise a PLAIN (transient) error — the receiverless
# path keeps the due record, so the reaper redelivers; the redelivered drive (attempt two) settles
# normally. Models a resumer killed mid-resume whose outcome the reaper recovers.
_TRANSIENT_ANSWER = "__held_transient__"


def _held_record_key(interaction_id: str) -> str:
    """The probe channel a resumed ``held_run`` drive records its answer and restored chain on."""
    return f"e2e:rec:held_run:{interaction_id}"


def _expiry_policy(value: str) -> Literal["kill", "resume"]:
    """Narrow a fixture caller's ``on_expiry`` to the ask expiry policy, raising on any other
    value — the tools take it as a plain ``str`` so the agent tool binder can resolve them."""
    if value == "kill":
        return "kill"
    if value == "resume":
        return "resume"
    raise ValueError(f"on_expiry must be 'kill' or 'resume', got {value!r}")


@tai42_app.tools.tool(tags={"e2e"})
async def held_route_park(marker: str) -> object:
    """A conversation tool-target that asks its CALLER and parks — the route-driven caller ask.

    Runs as the target of a ``target_kind=tool`` conversation route. It binds ``held_run_resume``
    as the resume continuation and async-asks ``to="caller"`` — so the caller ask parks on the
    route turn's subject, indexed under EACH of the turn's candidate keys, and the turn ends
    silently. RPUSHes ``{interaction_id, subject}`` onto ``e2e:rec:held_route_park:{marker}`` so a
    spec reads which interaction the silent turn parked on AND the turn subject (scope + candidate
    keys) a later door addresses to resume it cross-door. The turn runs under the route's bound
    execution identity, which the park stores as its continuation identity.
    """
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_kit.utils.state_context import current_state_context
    from tai42_skeleton.interactions import ask

    context = current_state_context()
    if context is None:
        raise RuntimeError("held_route_park ran with no ambient state context — a caller ask has no subject to park on")
    subject = {
        "target_kind": context.candidates.target_kind,
        "target_name": context.candidates.target_name,
        "by_kind": dict(context.candidates.by_kind),
    }

    with driving_as(continuation="held_run_resume"):
        expiry_at = datetime.now(UTC) + timedelta(seconds=3600)
        suspended = await ask(marker, to="caller", mode="async", expiry_at=expiry_at, payload={"reask": False})
    record = json.dumps({"interaction_id": suspended.interaction_id, "subject": subject, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as probe:
        await cast(Awaitable[int], probe.rpush(f"e2e:rec:held_route_park:{marker}", record))
    return suspended


@tai42_app.tools.tool(tags={"e2e"})
async def held_ask(
    marker: str, expiry_seconds: float, reask: bool = False, deeper: bool = False, on_expiry: str = "kill"
) -> object:
    """Async-ask the CALLER from the innermost frame and park, returning the suspended receipt.

    Binds ``held_run_resume`` as the resume continuation (plus a synthetic identity when the
    stack has bound none) and asks with ``to="caller"``. This tool is the ask-performing frame,
    so it is dropped from the ask's ``asked_by`` and the callers above it (``held_run``, and a
    deep re-park's ``held_deeper``) are what the chain records. ``reask``/``deeper`` ride the
    ask's ``payload`` so the resumed continuation reads them off the stored request; ``on_expiry``
    picks whether an unanswered park is killed (the chain torn down, one FAILED delivered) or
    resumed with the expiry marker.
    """
    from datetime import UTC, datetime, timedelta

    from tai42_skeleton.authz.execution_identity import get_execution_identity
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask

    identity = (
        None
        if get_execution_identity() is not None
        else CallerIdentity(user_id=_CALLER_PARK_IDENTITY, execution_key_fingerprint="e2e-caller-fp")
    )
    with driving_as(continuation="held_run_resume", identity=identity):
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        return await ask(
            marker,
            to="caller",
            mode="async",
            expiry_at=expiry_at,
            on_expiry=_expiry_policy(on_expiry),
            payload={"reask": reask, "deeper": deeper},
        )


@tai42_app.tools.tool(tags={"e2e"})
async def held_run(
    marker: str, reask: bool = False, deeper: bool = False, expiry_seconds: float = 3600, on_expiry: str = "kill"
) -> object:
    """Ask the CALLER (through ``held_ask`` one frame down) and park, returning the receipt.

    ``reask`` makes the resumed drive ask a SECOND time instead of finalizing, so the restored
    chain is observable on the re-park's ``asked_by``; ``deeper`` makes that second ask fire one
    frame lower (through ``held_deeper``) so the deeper tool's name is pushed onto the chain.
    ``on_expiry`` picks the unanswered-park policy — ``"kill"`` (the default) or ``"resume"``.
    """
    return await tai42_app.tools.run_tool(
        "held_ask",
        {
            "marker": marker,
            "expiry_seconds": expiry_seconds,
            "reask": reask,
            "deeper": deeper,
            "on_expiry": on_expiry,
        },
    )


@tai42_app.tools.tool(tags={"e2e"})
async def held_deeper(marker: str, expiry_seconds: float) -> object:
    """Ask the CALLER (through ``held_ask`` one frame down) from a deeper frame, so this tool's
    name is pushed onto the re-park's ``asked_by`` (depth two). Never re-parks again."""
    return await tai42_app.tools.run_tool(
        "held_ask", {"marker": marker, "expiry_seconds": expiry_seconds, "reask": False, "deeper": False}
    )


@tai42_app.tools.tool(tags={"e2e"})
async def held_run_resume(interaction_id: str, answer: object) -> object:
    """Re-drive a resumed ``held_run`` with the answer, under the restored call chain.

    Reads the resumed ask's stored ``payload`` for whether to re-park (and how deep), RPUSHes
    ``{"answer", "chain", "pid"}`` onto the probe channel — ``chain`` is the restored
    ``asked_by`` this drive runs under, proving the continuation tool's own name is not in it —
    then either finalizes by returning the answer, or asks the caller a SECOND time (through
    ``held_ask`` directly, or through ``held_deeper`` one frame lower) and returns the receipt.
    """
    from collections.abc import Awaitable
    from typing import cast

    from tai42_contract.tools import current_call_chain
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.interactions import InteractionStore, interactions_settings

    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        state = await store.get_state(r, interaction_id)
        if state is None:
            raise RuntimeError(f"held_run_resume: {interaction_id!r} names no stored caller ask")
        request_payload = state.request.payload or {}
    chain = list(current_call_chain())
    record = json.dumps({"answer": answer, "chain": chain, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as probe:
        await cast(Awaitable[int], probe.rpush(_held_record_key(interaction_id), record))
        if answer == _TRANSIENT_ANSWER:
            attempts = await cast(Awaitable[int], probe.incr(f"e2e:held:attempts:{interaction_id}"))
            if attempts == 1:
                # A PLAIN raise on the first drive: the receiverless path keeps the due record and
                # the reaper redelivers. The redelivered drive (attempts >= 2) settles below,
                # re-entering under the restored ``due.asked_by`` chain.
                raise RuntimeError("held_run resume transient failure — the reaper must redeliver")

    if answer == _FAIL_ANSWER:
        # A failure-stamped terminal: the ladder reads its status and delivers FAILED, so a
        # receiverless resume leaves a ``failed`` waiting outcome a later take fails on.
        return {"status": "failed", "reason": "resume-failed", "chain": chain}
    if not bool(request_payload.get("reask")):
        return {"answer": answer, "chain": chain}
    marker = f"{state.request.question}:reasked"
    if bool(request_payload.get("deeper")):
        return await tai42_app.tools.run_tool("held_deeper", {"marker": marker, "expiry_seconds": 3600.0})
    return await tai42_app.tools.run_tool(
        "held_ask", {"marker": marker, "expiry_seconds": 3600.0, "reask": False, "deeper": False}
    )


@tai42_app.tools.tool(tags={"e2e"})
async def caller_relay(payload: object) -> dict:
    """Resume the single parked caller ask on the CURRENT run's subject with ``payload``.

    Lists the subject's parked interactions, resumes the one ``asking`` caller ask with
    ``payload`` through the generic resume tool, and returns the resumed run's outcome. Raises
    loudly when the subject holds anything other than exactly one caller ask to resume, so a
    mis-sequenced spec fails visibly rather than resuming the wrong interaction.
    """
    entries = await tai42_app.interactions.list_parked()
    asks = [entry for entry in entries if entry.status == "asking" and entry.to == "caller"]
    if len(asks) != 1:
        raise RuntimeError(
            f"caller_relay expects exactly one caller ask on the subject, found {len(asks)}: "
            f"{[entry.id for entry in asks]}"
        )
    outcome = await tai42_app.interactions.resume_parked(asks[0].id, payload)
    return outcome.model_dump(mode="json")


@tai42_app.tools.tool(tags={"e2e"})
async def caller_list() -> list[dict[str, object]]:
    """List the caller asks parked on the CURRENT run's subject, each as ``{id, status,
    asked_by}``. A run on another subject sees an empty list — the subject scopes the read."""
    entries = await tai42_app.interactions.list_parked()
    return [
        {"id": entry.id, "status": entry.status, "asked_by": entry.asked_by}
        for entry in entries
        if entry.to == "caller"
    ]


@tai42_app.tools.tool(tags={"e2e"})
async def caller_resume_id(interaction_id: str, payload: object) -> dict:
    """Resume the named parked caller ask on the CURRENT run's subject with ``payload``.

    Unlike ``caller_relay`` this addresses an id directly, so a bad id reaches the generic
    resume tool and raises (nothing on the subject is touched) — the loud-failure path a spec
    drives to prove a mis-addressed resume cancels nothing.
    """
    outcome = await tai42_app.interactions.resume_parked(interaction_id, payload)
    return outcome.model_dump(mode="json")


@tai42_app.tools.tool(tags={"e2e"})
async def caller_cancel() -> dict:
    """Whole-chain kill every caller ask parked on the CURRENT run's subject.

    Lists the subject's caller asks and cancels them through the generic cancel tool, so a
    run a door started with a receiver is delivered its single FAILED. Returns the visit
    outcome naming the ids cancelled; raises loudly when the subject holds no caller ask.
    """
    entries = await tai42_app.interactions.list_parked()
    ids = [entry.id for entry in entries if entry.status == "asking" and entry.to == "caller"]
    if not ids:
        raise RuntimeError("caller_cancel found no caller ask on the subject to cancel")
    outcome = await tai42_app.interactions.cancel_parked(ids)
    return outcome.model_dump(mode="json")


@tai42_app.tools.tool(tags={"e2e"})
async def caller_resume_detached(payload: object) -> dict:
    """Resume the subject's one caller ask WITHOUT a receiver — a hook/schedule stand-in.

    Drives the shared visit with ``receives_outcome=False``, so the resumed run's terminal is
    NOT taken here; it waits on the subject for a later run to take. Returns the visit outcome
    (which carries no result under a receiverless resume). Raises when the subject holds other
    than one caller ask.
    """
    from tai42_contract.interactions import ResumeItem
    from tai42_skeleton.interactions.visit import visit

    entries = await tai42_app.interactions.list_parked()
    asks = [entry for entry in entries if entry.status == "asking" and entry.to == "caller"]
    if len(asks) != 1:
        raise RuntimeError(
            f"caller_resume_detached expects exactly one caller ask, found {len(asks)}: {[e.id for e in asks]}"
        )
    outcome = await visit(
        target_name="caller_resume_detached",
        cancel=[],
        resume=[ResumeItem(id=asks[0].id, payload=payload)],
        start=None,
        extras={},
        receives_outcome=False,
    )
    return outcome.model_dump(mode="json")


@tai42_app.tools.tool(tags={"e2e"})
async def caller_take() -> dict:
    """Take the single resolved run's waiting outcome off the CURRENT run's subject.

    Finds the one ``finished``/``failed`` waiting entry (keyed by the resolved run's completion,
    not the original ask) and resumes it with no payload — the TAKE path — so a ``finished``
    outcome's result is returned and a ``failed`` one raises into this run exactly as the
    resolved run failed. The take is the receiver a receiverless resume left the outcome for.
    Raises when the subject holds other than one waiting outcome.
    """
    entries = await tai42_app.interactions.list_parked()
    waiting = [entry for entry in entries if entry.status in ("finished", "failed")]
    if len(waiting) != 1:
        raise RuntimeError(
            f"caller_take expects exactly one waiting outcome on the subject, found {len(waiting)}: "
            f"{[(entry.id, entry.status) for entry in waiting]}"
        )
    outcome = await tai42_app.interactions.resume_parked(waiting[0].id)
    return outcome.model_dump(mode="json")
