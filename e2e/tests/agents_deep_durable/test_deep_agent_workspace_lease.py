"""The per-workspace lease BUSY path — a cross-worker seam with no reachable e2e door here.

The intent (§C4): two near-simultaneous THREADED turns of one ``thread_id`` race the shared Redis
workspace lease (``agent:park:wslock:<workspace_key>`` SET-NX over the ``TAI_AGENTS_REDIS_*`` park
store); one wins, the other gets the constant-message ``WorkspaceLeaseHeldError`` from the lease
``__aenter__`` BEFORE any session or ``.creds`` exists (so it leaks nothing), and the winner's
``finally`` compare-and-deletes so a later turn acquires.

Why this is not reachable through a door mounted on the foundation stacks — a genuine FOUNDATION
composition gap, flagged (not silently skipped):

* The lease is taken ONLY for a TRUSTED-THREAD drive (``leased(thread_id=<trusted>)``). By the
  deliberate security invariant, a durable-workspace agent exposes NO caller-facing ``thread_id``
  field (``claude_code``/``langchain_deep_agent`` both), so ``thread_id`` arrives ONLY as a trusted
  param from the conversation-bridge turn (``astream(thread_id=...)``). A caller-facing MCP/SSE run
  gets a fresh EPHEMERAL ``uuid4`` workspace that takes NO lease — so no MCP/SSE door ever contends
  the lease.

* The ONE foundation stack that reaches the trusted-thread bridge door AND registers
  ``langchain_deep_agent`` (``build_bridge_stack``) installs NO sandbox provider, so a deep-agent
  RUN turn there raises ``SandboxUnavailableError`` before a lease is ever taken. The fake-sandbox
  durable stack has the provider but no bridge door. No foundation stack composes a bridge door
  WITH a sandbox provider WITH the park Redis.

* Even given such a stack, two concurrent same-``thread_id`` turns serialize FIRST on the
  conversation per-thread FIFO (``run_reserved``'s cross-worker thread lease -> ``ThreadBusyError``)
  and a concurrent park-resume serializes on the park drive-lease
  (``AgentResumeDriveInProgressError``) — both BEFORE the workspace lease, so its busy path stays
  masked from every reachable door.

The busy path is therefore a defense-in-depth internal seam, proven at the plugin unit level
(``plugins/agents/tests/test_langchain_deep_agent_durable.py::
test_leased_loser_creates_no_session_and_writes_no_creds`` — the lease-loser creates no session and
writes no creds). Recorded here as a skip so the coverage decision is explicit and traceable.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "the workspace-lease busy path has no reachable e2e door on the foundation stacks: the lease "
        "is taken only for a trusted-thread (bridge) drive, the one bridge-door stack registering "
        "langchain_deep_agent installs no sandbox provider, and the conversation FIFO / park "
        "drive-lease serialize before the workspace lease — it is a defense-in-depth seam proven at "
        "the plugin unit level (test_leased_loser_creates_no_session_and_writes_no_creds)"
    )
)


def test_workspace_lease_busy_path_is_unit_covered() -> None:
    """Placeholder for the lease busy path — see the module docstring for why it is unreachable
    through a foundation-stack door and where it is unit-covered."""
