"""Sandbox contract: the neutral session models, the resolved
:class:`SandboxPolicy`, the error family, and the :class:`Sandbox` /
:class:`SandboxSession` / :class:`SandboxExecHandle` ABCs.

WHAT THE CONTRACT CARRIES: the shape of a session request/result, the security
policy the kit enforces, the failure family, and the provider face — no logic.
WHAT THE KIT OWNS: the shared session ledger, TTL/reap bookkeeping, and the
session-create policy chokepoint (a ``Sandbox`` / ``SandboxSession`` base a
provider extends). WHAT A PROVIDER IMPLEMENTS: only its runtime I/O — creating
session resources and running ``exec`` / file transfers against them.
"""

from __future__ import annotations

from tai42_contract.sandbox.base import Sandbox, SandboxExecHandle, SandboxSession
from tai42_contract.sandbox.errors import (
    SandboxError,
    SandboxExecTimeoutError,
    SandboxSessionNotFoundError,
    SandboxSpecRejectedError,
    SandboxUnavailableError,
)
from tai42_contract.sandbox.models import (
    ExecResult,
    SandboxDurability,
    SandboxIsolation,
    SandboxNetwork,
    SandboxSessionInfo,
    SandboxSessionSpec,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_contract.sandbox.policy import (
    SandboxPolicy,
    isolation_strength,
    network_openness,
)

__all__ = [
    "ExecResult",
    "Sandbox",
    "SandboxDurability",
    "SandboxError",
    "SandboxExecHandle",
    "SandboxExecTimeoutError",
    "SandboxIsolation",
    "SandboxNetwork",
    "SandboxPolicy",
    "SandboxSession",
    "SandboxSessionInfo",
    "SandboxSessionNotFoundError",
    "SandboxSessionSpec",
    "SandboxSpecRejectedError",
    "SandboxStreamChunk",
    "SandboxStreamExit",
    "SandboxUnavailableError",
    "isolation_strength",
    "network_openness",
]
