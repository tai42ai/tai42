"""Agent async ``ask_user`` park/resume: middleware, durable index, and resume driver.

The three pieces behind a park-capable agent run that async-parks on an ``ask_user``:

* :class:`AsyncParkMiddleware` — the ``before_model`` hook that interrupts the loop once
  per super-step of async-ask parks and substitutes their answers back on resume.
* the durable park :mod:`~tai42_agents._internal.park.index` — reverses a parked
  interaction id back to its parked run, in the agents plugin's own Redis.
* the :mod:`~tai42_agents._internal.park.capability`, :mod:`~tai42_agents._internal.park.persist`,
  :mod:`~tai42_agents._internal.park.drive`, and :mod:`~tai42_agents._internal.park.resume`
  modules — park capability, the persist seam, the drive-side finalizer, and the
  ``agent_resume`` continuation the flow-blind platform fires.
* the :mod:`~tai42_agents._internal.park.chain` delivery tool — the other end of a CHAINED
  park: a nested run's terminal, reversed through the same barrier back into the loop.

Everything agent-park-specific lives here; the platform contract gains only the generic
suspension marker + ``SuspendedFinal`` vocabulary.
"""

from __future__ import annotations

from tai42_agents._internal.park.capability import (
    DURABLE_CHECKPOINT_PROVIDERS,
    ParkIdentity,
    assert_park_capable,
    build_park_identity,
)
from tai42_agents._internal.park.chain import (
    CHAINED_PARK_DELIVERY_TOOL_NAME,
    deliver_chained_park,
    register_chained_park_tool,
)
from tai42_agents._internal.park.drive import (
    bind_resume_per_step,
    collect_pending_interrupts,
    detach_dead_chains,
    finalize_drive,
    park_continuation,
    park_drive,
    park_step_binding,
)
from tai42_agents._internal.park.lease import (
    LEASE_HEADROOM_SECONDS,
    WSLOCK_KEY_PREFIX,
    workspace_lease,
)
from tai42_agents._internal.park.middleware import AsyncParkMiddleware
from tai42_agents._internal.park.persist import persist_park
from tai42_agents._internal.park.resume import (
    AGENT_RESUME_TOOL_NAME,
    agent_resume,
    fire_park_failed_completion,
    is_suspended_receipt,
)
from tai42_agents._internal.park.resume_tool import register_agent_resume_tool

__all__ = [
    "AGENT_RESUME_TOOL_NAME",
    "CHAINED_PARK_DELIVERY_TOOL_NAME",
    "DURABLE_CHECKPOINT_PROVIDERS",
    "LEASE_HEADROOM_SECONDS",
    "WSLOCK_KEY_PREFIX",
    "AsyncParkMiddleware",
    "ParkIdentity",
    "agent_resume",
    "assert_park_capable",
    "bind_resume_per_step",
    "build_park_identity",
    "collect_pending_interrupts",
    "deliver_chained_park",
    "detach_dead_chains",
    "finalize_drive",
    "fire_park_failed_completion",
    "is_suspended_receipt",
    "park_continuation",
    "park_drive",
    "park_step_binding",
    "persist_park",
    "register_agent_resume_tool",
    "register_chained_park_tool",
    "workspace_lease",
]
