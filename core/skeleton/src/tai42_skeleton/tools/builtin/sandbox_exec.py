"""The ``sandbox_exec`` base tool: run a command in a disposable sandbox session.

A generic, domain-agnostic base tool. It acquires a session through the ONE acquisition
chokepoint (``tai42_app.sandboxes.require_sandbox()`` — a loud ``SandboxUnavailableError``
on every door with no provider), runs ``argv`` in it via ``SandboxSession.exec``, and
returns the :class:`~tai42_contract.sandbox.ExecResult`. A DELIVERY names concrete tools
as PRESETS over it; the base itself carries no domain knowledge.

At load it DECLARES its input-schema support through the ``tai42_app`` handle (so a
preset's ``input_schema`` becomes the exposed tool's input contract, delivered under the
``input`` arg) and its ``fenced`` registration tier. The ``fenced`` tier is admin-only for
BOTH authoring a ``sandbox_exec`` preset AND running the tool: the generic run-time fence
at the tool-run chokepoint refuses a non-admin on every door, so the base carries no fence
of its own, and a preset authored over it inherits the fence at run time.

The base tool reads NO policy knobs and builds NO policy of its own: it acquires the
session through the SAME kit session-create policy chokepoint, so the platform policy
(the network ceiling, the isolation floor, the durable gate) governs it AUTOMATICALLY.
``isolation`` is LEFT UNSET so it inherits the platform floor; ``network`` defaults to a
FIXED ``"egress"`` (the OPEN default posture, NOT a read of the egress knob) — the kit
create ceiling still loudly rejects anything looser than the operator's egress.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from tai42_contract.app import tai42_app
from tai42_contract.presets import PresetInputSchemaSupport
from tai42_contract.sandbox import ExecResult, SandboxSessionSpec

# The base-tool argument a preset's validated structured input is delivered under.
_PAYLOAD_ARG = "input"


@tai42_app.tools.tool(tags={"sandbox"})
async def sandbox_exec(
    argv: list[str],
    *,
    image: str,
    input: dict[str, Any] | None = None,
    network: Literal["none", "internal", "egress"] = "egress",
    cpu: float | None = None,
    memory_mb: int | None = None,
    ttl_seconds: int = 300,
    timeout_seconds: float = 300,
    workspace_key: str = "sandbox-exec",
    durability: Literal["ephemeral", "persistent"] = "ephemeral",
) -> ExecResult:
    """Run ``argv`` in a disposable sandbox session and return its exit code + output.

    Args:
        argv: The command and its arguments to run to completion.
        image: The exact runnable image reference to run in.
        input: A structured object delivered to the command as JSON on stdin (a preset's
            ``input_schema`` routes its validated object here).
        network: The session network mode; defaults to the OPEN ``egress`` posture. The
            kit create ceiling loudly rejects a value looser than the operator's egress.
        cpu: Optional CPU cap.
        memory_mb: Optional memory cap in MiB.
        ttl_seconds: The session's idle reap deadline in seconds.
        timeout_seconds: The per-``exec`` wall-clock timeout in seconds.
        workspace_key: The session's workspace identity (a durable volume name for a
            persistent session).
        durability: ``ephemeral`` (a scratch workspace) or ``persistent`` (a durable
            workspace, refused loudly by the kit when the operator disabled durable).

    Returns:
        The command's :class:`ExecResult` (exit code, stdout, stderr).
    """
    sandbox = tai42_app.sandboxes.require_sandbox()
    spec = SandboxSessionSpec(
        image=image,
        workspace_key=workspace_key,
        durability=durability,
        network=network,
        # Left UNSET so the session inherits the platform isolation floor at the kit
        # create seam; the base tool reads no policy of its own.
        isolation=None,
        cpu=cpu,
        memory_mb=memory_mb,
        ttl_seconds=ttl_seconds,
    )
    stdin = json.dumps(input).encode() if input is not None else None
    session = await sandbox.create_session(spec)
    try:
        return await session.exec(argv, stdin=stdin, timeout_seconds=timeout_seconds)
    finally:
        # A completed exec leaves nothing to keep: tear the session down promptly rather
        # than wait for the reaper. A persistent workspace survives on its durable volume.
        await session.destroy()


# Declare the tool mechanisms at load, through the handle (the write-validator pattern):
# a preset's ``input_schema`` becomes the exposed tool's input contract (routed into the
# ``input`` arg), and the ``fenced`` tier admin-fences both authoring a preset over the
# tool and running it (enforced generically at the tool-run chokepoint).
tai42_app.presets.register_input_schema_support("sandbox_exec", PresetInputSchemaSupport(payload_arg=_PAYLOAD_ARG))
tai42_app.tools.register_tier("sandbox_exec", "fenced")
