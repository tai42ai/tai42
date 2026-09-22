"""The async park's resume binding and re-park notice.

Capture the resume continuation + execution identity + fingerprint +
state-context + bound thread when a park is raised, and notify a chained caller
when the run it addresses re-parks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    PARK_COMPLETION_THREAD_KEY,
    get_park_completion,
    get_resume_continuation_tool,
    repark_notice,
)
from tai42_contract.states import StateContext
from tai42_contract.tools import get_run_delivery, get_run_delivery_id

logger = logging.getLogger(__name__)

# The park-completion context key a tool/flow route target binds the delivery thread under
# (see ``conversations.turn.tool_turn._run_tool_turn``'s ``set_park_completion``). The CONTRACT names it:
# it is the one reserved field inside an otherwise-opaque completion context, so the read here,
# the turn-layer write, and any party that composes a context over another (a chained dispatch
# carries it up to the new top level) all agree on the field.
_PARK_COMPLETION_THREAD_KEY = PARK_COMPLETION_THREAD_KEY


@dataclass(frozen=True)
class AsyncParkBinding:
    """The fields an async park's resume binding carries.

    The resume continuation tool, the execution identity + key fingerprint to
    rebind it as, the parking turn's state context, and the bound conversation
    thread (all ``None`` for a sync ask).
    """

    continuation_tool: str | None = None
    continuation_identity: str | None = None
    continuation_fingerprint: str | None = None
    continuation_state_context: StateContext | None = None
    park_thread_id: str | None = None
    # The RUN's delivery identity and out-of-band address, read from the ambient
    # run-delivery context the outermost ``tool_call_frame`` bound. The run_delivery_id
    # is always present for a parkable run (the persist raises without one); the address
    # is ``None`` for a receiver-less run. Both are stored verbatim on the request.
    run_delivery_id: str | None = None
    delivery: tuple[str | None, dict[str, Any] | None] | None = None


def resolve_async_continuation(to: Literal["user", "caller"] = "user") -> AsyncParkBinding:
    """Resolve the async park's resume binding, raising loudly when a driver/identity is missing.

    Captures the resume tool + execution identity + fingerprint + state-context +
    bound thread, and the RUN's delivery identity + address. An async ask with no bound
    driver, no identity to rebind it as, or (a platform bug) no ambient run-delivery
    identity is a failure that must fail loudly, never persist a question no answer/expiry
    could resume or deliver. A ``caller`` ask additionally requires an ambient state context
    (its outcome is subject-tracked back through the parking run's subject).
    """
    continuation_tool = get_resume_continuation_tool()
    if continuation_tool is None:
        raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
    # Function-local: a module-level edge from here into ``authz`` closes an
    # import cycle (authz → access_control.backend) that crashes any process
    # importing ``access_control`` first.
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    identity = get_execution_identity()
    if identity is None or identity.user_id is None:
        raise RuntimeError("async ask requires a bound execution identity to rebind the continuation as")
    # Stashed alongside the identity so the answer/expiry path rebinds the continuation
    # under the SAME fire authority the ask ran under; gate-off fires carry no
    # fingerprint, recorded as "" (the bind ignores it there).
    continuation_fingerprint = identity.execution_key_fingerprint or ""
    # The parking turn's ambient state context, captured here inside that turn so the
    # resume re-deposits the original door's subject + write provenance. Function-local
    # import: continuation → store → this module would close a cycle.
    from tai42_skeleton.interactions.continuation import _current_state_context_for_park

    continuation_state_context = _current_state_context_for_park()
    if to == "caller" and continuation_state_context is None:
        # A caller ask's outcome is handed back through the parking run's subject, read
        # from the ambient state context; with none there is no subject to track it on.
        raise RuntimeError("a caller ask (to='caller') requires an ambient state context")
    # The bound conversation thread (from the turn layer's park-completion / bridge
    # context), so this park joins its thread's reverse index and a thread delete can
    # cancel it. ``None`` outside a bound conversation turn.
    park_thread_id = bound_park_thread_id()
    # The RUN's delivery identity + address, bound once at the outermost run start on the
    # ambient run-delivery context. A parkable run ALWAYS started through the frame that
    # mints it, so a missing id is a platform bug — raise, never persist a park the
    # delivery chokepoint could not key.
    run_delivery_id = get_run_delivery_id()
    if run_delivery_id is None:
        raise RuntimeError("async ask parked with no ambient run_delivery_id (the run started outside a call frame)")
    raw_delivery = get_run_delivery()
    delivery = (
        (raw_delivery[0], dict(raw_delivery[1]) if raw_delivery[1] is not None else None)
        if raw_delivery is not None
        else None
    )
    return AsyncParkBinding(
        continuation_tool=continuation_tool,
        continuation_identity=identity.user_id,
        continuation_fingerprint=continuation_fingerprint,
        continuation_state_context=continuation_state_context,
        park_thread_id=park_thread_id,
        run_delivery_id=run_delivery_id,
        delivery=delivery,
    )


def bound_park_thread_id() -> str | None:
    """The conversation thread this async park belongs to, or ``None`` when none is bound.

    A background tool run or a direct/agent-less ask has none. Two turn-layer bindings expose it and
    this reads whichever is set, staying engine-agnostic (it names no engine, only the
    generic bindings):

    * a TOOL/flow route target binds the thread as the park-completion context's
      ``delivery_thread_id`` (``conversations.turn.tool_turn._run_tool_turn``), read here off
      :func:`get_park_completion`;
    * an AGENT route target runs inside the bridge turn context that carries the thread
      (``conversations.turn.agent_turn._run_agent_turn``), read here off ``current_bridge_turn``. Its own
      park-completion context is keyed by ITS delivery tool's routing parameter, not this one,
      and is deliberately not read here — a completion context is opaque, so only the ONE key
      this module owns is ever looked up in it.

    Never fabricates a thread id — an unbound park indexes nothing. A park raised where neither
    binding is set is therefore not thread-indexed and not cascade-cancellable. Three cases sit on
    that boundary, and they do NOT all behave alike:

    * a background tool run (no turn at all) — unbound, unindexed. Nothing was indexed before this
      index existed, so this is a floor, not a regression.
    * a RE-PARK during an out-of-band resume drive, which delivers via ``deliver_*_completion``
      without re-wrapping the turn. A resuming driver rebinds the stored completion around the
      drive, so a TOOL-route re-park is indexed (its rebound context carries
      ``delivery_thread_id``) while an AGENT-route re-park is NOT: its context is keyed by ITS
      delivery tool's parameter, which this module deliberately does not read, and there is no
      bridge turn context out of band. The asymmetry follows from the opacity rule above rather
      than from any judgement that one deserves indexing more.
    * a nested park inside an AGENT running as a TOOL route's target. The agent binds its own
      address over every tool it dispatches, so no nested driver can capture the address the
      agent's answer is owed to; a tool turn establishes no bridge turn context. Whether such a
      park is indexed follows from what the agent bound: a CHAINED dispatch composes a context
      that carries this reserved field up from the one it wrapped, so the nested park is indexed
      to the same thread; an unchained one clears the binding, and the park is unindexed.

    Closing the open cases means the resume drive (and the tool-turn door) establishing the thread
    binding in their own right, left to a follow-up.
    """
    _completion_tool, completion_ctx = get_park_completion()
    if completion_ctx is not None:
        candidate = completion_ctx.get(_PARK_COMPLETION_THREAD_KEY)
        if isinstance(candidate, str) and candidate:
            return candidate
    # Function-local, mirroring the ``get_execution_identity`` import above: a module-level
    # edge from interactions into conversations would couple two peer packages at import time.
    from tai42_skeleton.conversations.turn_context import current_bridge_turn

    bridge = current_bridge_turn()
    if bridge is not None:
        return bridge.thread_id
    return None


async def notify_repark(expiry_at: datetime | None, *, interaction_id: str) -> None:
    """Tell a CHAINED completion binding that the run it addresses just parked on a new ask.

    A caller whose own suspension horizon was inherited from that run can then refresh it.

    Fired only when :func:`repark_notice` reports a chained binding — every other completion
    binding (and no binding at all) is silent, so no delivery tool ever sees a fire it has no
    horizon to answer. BEST-EFFORT by construction: the notice refreshes a horizon, it never
    carries an answer, so a failing notifier is logged and swallowed rather than turning a
    successfully persisted park into a failed ``ask``. The cost of a lost notice is a
    caller whose horizon stays at the previous ask's deadline.
    """
    notice = repark_notice(expiry_at)
    if notice is None:
        return
    tool, payload = notice
    try:
        await tai42_app.tools.run_tool(tool, payload)
    except Exception:
        logger.warning(
            "ask: the re-park horizon notice to %r failed for interaction %s; the chained caller "
            "keeps its previous horizon",
            tool,
            interaction_id,
            exc_info=True,
        )
