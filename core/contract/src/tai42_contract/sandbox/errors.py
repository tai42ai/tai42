"""Errors the sandbox contract raises.

One family so a consumer catches :class:`SandboxError` and reaches every
failure. Messages are CONSTANT-SAFE: they never interpolate a session's ``env``
values (a timeout carries partial-output LENGTHS, never content).
"""

from __future__ import annotations


class SandboxError(Exception):
    """Base for every sandbox failure."""


class SandboxUnavailableError(SandboxError):
    """No sandbox provider is registered.

    Raised by the facet ``require_sandbox()`` acquisition chokepoint so every
    consumer catches this ONE type when no provider backs the seam.
    """


class SandboxSessionNotFoundError(SandboxError):
    """No live session has the requested id."""

    def __init__(self, session_id: str):
        super().__init__(f"sandbox session {session_id!r} not found")
        self.session_id = session_id


class SandboxExecTimeoutError(SandboxError):
    """An ``exec`` / ``exec_start`` exceeded its ``timeout_seconds``.

    Carries the partial-output LENGTHS (never the content — output may hold
    secrets read from ``env``) so a caller can log the shape of what was produced
    before the kill.
    """

    def __init__(self, *, timeout_seconds: float, stdout_len: int, stderr_len: int):
        super().__init__(
            f"sandbox exec exceeded its {timeout_seconds}s timeout "
            f"(stdout {stdout_len} bytes, stderr {stderr_len} bytes)"
        )
        self.timeout_seconds = timeout_seconds
        self.stdout_len = stdout_len
        self.stderr_len = stderr_len


class SandboxSpecRejectedError(SandboxError):
    """A :class:`~tai42_contract.sandbox.SandboxSessionSpec` cannot be honored.

    ONE error for two causes the message distinguishes: EITHER the provider
    cannot enforce the spec (e.g. ``persistent`` on a provider without durable
    storage, an unenforceable cap) OR the spec violates the operator policy at the
    kit session-create chokepoint (a ``network`` looser than the egress ceiling,
    an ``isolation`` below the floor, ``persistent`` while durable is off). The
    message names which; the family never silently downgrades a rejected spec.
    """
