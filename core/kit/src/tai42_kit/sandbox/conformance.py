"""An importable conformance suite any sandbox provider runs against a live instance.

The bar a provider must clear is "map every neutral field onto your runtime, or reject what you
cannot honor". That bar is only real if it is executable, so it lives here as a driveable suite
rather than a review checklist: a provider's tests build a live
:class:`~tai42_kit.sandbox.base.ManagedSandbox`, hand it to :func:`run_sandbox_conformance`,
and get the same behaviour every consumer relies on — session lifecycle, the ``spec.env``
credential channel, a one-shot exec's stdin, the output its timeout counts and the output a
process it started holds past its command's exit, read to its end, the interactive byte-stream
seam (the output received before a ``kill()`` read after it, a read past the deadline of an exec
ended within it, its output past what the provider's transport buffers and a reader slower than
any grace included, a kill past the exec's deadline,
the deadline's kill of an exec nothing reads and of one that closed its output, the output its
timeout counts, read or not, and the timeout of an exec whose output a process it started still
holds at its deadline included), the stop of what a command left in its process group past its
own exit, at its deadline and by ``kill()``, workspace-path resolution, consumer-label
round-trip, TTL reap, and loud spec rejection.

The checks live by concern: the session-lifecycle checks in
:mod:`~tai42_kit.sandbox.conformance_session`, the exec seam checks in
:mod:`~tai42_kit.sandbox.conformance_exec`, the checks of what an exec's deadline does in
:mod:`~tai42_kit.sandbox.conformance_deadline`, the checks that a stop kills what a command left
in its process group in :mod:`~tai42_kit.sandbox.conformance_group_stop`, and the harness they all
drive with — the provider's :class:`~tai42_kit.sandbox.conformance_harness.SandboxConformanceConfig`,
the permissive policy the suite binds, the specs it creates and the stop of a descendant a case
started — in :mod:`~tai42_kit.sandbox.conformance_harness`.

pytest-free and dependency-free, so it ships in the wheel and any test runner can
call it.
"""

from __future__ import annotations

from tai42_kit.sandbox import (
    conformance_deadline,
    conformance_exec,
    conformance_group_stop,
    conformance_harness,
    conformance_session,
)
from tai42_kit.sandbox.base import ManagedSandbox


async def run_sandbox_conformance(
    sandbox: ManagedSandbox, config: conformance_harness.SandboxConformanceConfig
) -> None:
    """Drive the full suite against a live ``sandbox``.

    Binds a permissive policy first (an unbound policy is a loud programming
    error), then runs every step. An empty return is the certification: this
    provider gives every consumer the same session lifecycle, credential channel,
    one-shot stdin and timeout count, interactive seam (the output received before a kill, a
    late read, of output past the transport's buffer too and by a reader slower than any grace,
    a kill past the deadline, the deadline's own kill, of an
    exec nothing reads and of one that closed its output too, the output its timeout counts, past
    the transport's buffer too, and the timeout of an exec whose output a process it started
    still holds, included), the stop of what a command left in its group past its own exit,
    path resolution, label round-trip, TTL reap, and loud rejection.
    """
    sandbox.bind_policy(conformance_harness.permissive_policy())
    await conformance_exec.check_exec_and_env(sandbox, config)
    await conformance_exec.check_one_shot_large_stdin(sandbox, config)
    await conformance_exec.check_one_shot_unread_stdin(sandbox, config)
    await conformance_exec.check_interactive_exec(sandbox, config)
    await conformance_exec.check_kill_keeps_received_output(sandbox, config)
    await conformance_deadline.check_late_read_after_exit(sandbox, config)
    await conformance_deadline.check_late_read_of_large_output(sandbox, config)
    await conformance_deadline.check_slow_read_of_large_output(sandbox, config)
    await conformance_deadline.check_kill_after_deadline(sandbox, config)
    await conformance_session.check_file_transfer(sandbox, config)
    await conformance_session.check_workspace_path(sandbox, config)
    await conformance_session.check_labels_round_trip(sandbox, config)
    await conformance_session.check_touch_extends(sandbox, config)
    if config.check_persistent_survives:
        await conformance_session.check_persistent_survives_reap(sandbox, config)
    await conformance_session.check_reap_and_destroy(sandbox, config)
    await conformance_session.check_spec_rejection(sandbox, config)
    await conformance_deadline.check_exec_timeout(sandbox, config)
    await conformance_deadline.check_exec_timeout_counts_output(sandbox, config)
    await conformance_deadline.check_interactive_exec_timeout(sandbox, config)
    await conformance_deadline.check_interactive_timeout_counts_output(sandbox, config)
    await conformance_deadline.check_interactive_timeout_counts_large_output(sandbox, config)
    await conformance_deadline.check_unread_exec_killed_at_deadline(sandbox, config)
    await conformance_deadline.check_output_closed_exec_killed_at_deadline(sandbox, config)
    await conformance_deadline.check_output_held_after_exit_read_to_end(sandbox, config)
    await conformance_deadline.check_output_holder_at_deadline_times_out(sandbox, config)
    await conformance_deadline.check_signalled_exec_with_output_holder_times_out(sandbox, config)
    await conformance_group_stop.check_one_shot_deadline_kills_what_the_command_left_in_its_group(sandbox, config)
    await conformance_group_stop.check_interactive_deadline_kills_what_the_command_left_in_its_group(sandbox, config)
    await conformance_group_stop.check_kill_kills_what_the_command_left_in_its_group(sandbox, config)
