"""The sandbox session lifecycle, implemented once for every provider.

A provider implements only its runtime I/O — creating session resources and
running ``exec`` / file transfers against them — by subclassing
:class:`~tai42_kit.sandbox.base.ManagedSandbox` and
:class:`~tai42_kit.sandbox.session.ManagedSandboxSession`. Everything host-shaped
around that (the session ledger, TTL bookkeeping, generic ``reap`` /
``destroy_session``, orphan recovery, and the SESSION-CREATE POLICY CHOKEPOINT
that enforces the operator-resolved :class:`SandboxPolicy` before any provider
primitive runs) lives here. A session is either ephemeral — its workspace dies
with it — or persistent, binding a durable workspace volume that survives the
reap; the tier is chosen per session spec and honored uniformly across providers.
A provider whose execs run as host processes builds them on
:class:`~tai42_kit.sandbox.host_process.HostProcess` and its exec handle
:class:`~tai42_kit.sandbox.host_exec.HostExecHandle`, and runs a one-shot exec with
:func:`~tai42_kit.sandbox.host_exec.run_host_process`. Every interactive exec handle is bounded
by the one :class:`~tai42_kit.sandbox.exec_deadline.ExecDeadline` its exec fixes as it starts.
A provider whose execs run inside a
container and are killed through a sibling exec runs them under the scripts of
:mod:`~tai42_kit.sandbox.exec_group_scripts` and
:mod:`~tai42_kit.sandbox.exec_status_scripts`, reports each kill as a
:class:`~tai42_kit.sandbox.exec_kill.KillResult`
(:func:`~tai42_kit.sandbox.exec_status_scripts.kill_report`), runs a one-shot exec with
:func:`~tai42_kit.sandbox.sibling_kill_one_shot.run_one_shot`, and builds its exec handle on
:class:`~tai42_kit.sandbox.sibling_kill_handle.SiblingKillExecHandle`; it imports them from
those modules, which this package does not load, so a host-process provider never loads them.
A provider whose workspace mounts at a fixed path in the session's runtime resolves a
``cwd`` or file path against it with
:func:`~tai42_kit.sandbox.workspace_paths.resolve_workspace_path`. Every provider feeds a
one-shot exec's stdin while it reads the exec's output, with
:func:`~tai42_kit.sandbox.stdin_feed.read_while_feeding`. Cleanup that must finish though its
caller is cancelled (a cleanup kill, the teardown of a half-built session) is awaited through
:func:`~tai42_kit.sandbox.uncancellable_wait.wait_through_cancellation`, imported from its
module, which this package does not load either.

It lives in kit and not the skeleton because a provider plugin may not import the
skeleton, and not the contract because the contract carries no logic. Kit is the
only package both a plugin and the skeleton may import. ``SandboxPolicy`` and its
ordering helpers are CONTRACT-defined (the contract cannot import the kit); the
kit imports and CONSUMES them at the chokepoint.
"""

from tai42_kit.sandbox.base import (
    LABEL_DURABILITY,
    LABEL_SANDBOX,
    LABEL_WORKSPACE,
    ManagedSandbox,
)
from tai42_kit.sandbox.conformance import run_sandbox_conformance
from tai42_kit.sandbox.conformance_harness import SandboxConformanceConfig, permissive_policy
from tai42_kit.sandbox.exec_deadline import ExecDeadline
from tai42_kit.sandbox.host_exec import HostExecHandle, run_host_process
from tai42_kit.sandbox.host_process import HostProcess
from tai42_kit.sandbox.session import ManagedSandboxSession
from tai42_kit.sandbox.settings import SandboxDispatchSettings
from tai42_kit.sandbox.stdin_feed import read_while_feeding
from tai42_kit.sandbox.workspace_paths import resolve_workspace_path

__all__ = [
    "LABEL_DURABILITY",
    "LABEL_SANDBOX",
    "LABEL_WORKSPACE",
    "ExecDeadline",
    "HostExecHandle",
    "HostProcess",
    "ManagedSandbox",
    "ManagedSandboxSession",
    "SandboxConformanceConfig",
    "SandboxDispatchSettings",
    "permissive_policy",
    "read_while_feeding",
    "resolve_workspace_path",
    "run_host_process",
    "run_sandbox_conformance",
]
