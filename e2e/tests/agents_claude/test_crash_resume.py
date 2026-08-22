"""§3d — configurable crash-resume for a detached ``claude_code`` run (claude side).

Crash-resume (the ``crash_resume`` setting) re-invokes a recycled DETACHED run at-least-once:
a detached run enters via the TRIGGER door (which binds an execution identity and keeps its
tools), is recorded, and — when its per-run liveness key lapses after a worker recycle — the
FIRST read of the stale-``RUNNING`` record hits the liveness->``lost`` reconciler, which, seeing
the flag stamped from the agent's run-tool registration meta (``meta={"tai42/crash_resume":
True}``), RE-INVOKES ``run_recorded`` with the record's persisted ``arguments`` REBOUND to the
record's stored ``user_id`` (never the reader's identity), driving the ephemeral run from
scratch to a terminal record; a flagless tool goes quietly ``lost``, unchanged.

That flow needs, all at once: the trigger door + a detached, RECORDED run (a backend worker and
its run recorder) + a bound execution identity to persist and rebind + the liveness->lost
reconciler + the ``fresh_stack`` recycle seam. ``build_claude_agent_stack`` runs ONE worker with
NO backend, NO trigger router, and NO identity provider, so none of those seams are reachable
on it — the claude crash-resume flow cannot be driven deterministically on this stack within
this suite's scope.

This module is therefore skipped with that reason so the gap is explicit, not silent. Crash-resume
has NO e2e coverage on either durable-session agent: the claude leg cannot be driven here, and the
deep suite composes no crash-resume leg (``build_deep_agent_durable_stack`` runs with
``run_backend=False``, and ``tests/agents_deep_durable/`` carries no crash-resume test). The
mechanism is covered UNIT-only, where it is deterministic: the ``crash_resume`` registration meta
is asserted in ``plugins/agents/tests/test_crash_resume_meta.py`` (each durable-session agent
declares ``meta={"tai42/crash_resume": <setting>}`` on its run tool), and the reconciler's
re-invoke / identity-rebind / flagless-``lost`` branch is asserted in
``core/skeleton/tests/operations/test_crash_resume.py``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="claude crash-resume needs the trigger door + a detached recorded run (a backend "
    "worker + run recorder) + a bound execution identity + the liveness->lost reconciler + the "
    "recycle seam; build_claude_agent_stack wires none of them (one worker, no backend, no "
    "triggers, no identity provider). Crash-resume has no e2e coverage on either durable-session "
    "agent (the deep suite composes no crash-resume leg either); it is covered UNIT-only, in "
    "plugins/agents/tests/test_crash_resume_meta.py (the crash_resume registration meta) and "
    "core/skeleton/tests/operations/test_crash_resume.py (the liveness->lost re-dispatch branch)."
)


def test_claude_crash_resume_re_dispatches_a_recycled_detached_run() -> None:  # pragma: no cover - documented gap
    ...
