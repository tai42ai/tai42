"""Vendor-neutral data shapes for the sandbox contract.

A sandbox is an isolated, disposable execution environment a consumer runs code
in. These models describe WHAT a session must be, never HOW a provider realizes
it: the provider maps each neutral field onto its runtime or REJECTS the ones it
cannot satisfy, exactly as the backend contract keeps the app runtime-agnostic.
Credentials ride the session ONLY through ``env`` (every value a ``SecretStr``
kept out of repr/logs), and the geometry knobs (``network`` / ``isolation``) are
neutral tiers the provider translates.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

# The runtime network mode a session runs under, ordered open-ness weakest→
# strongest (``none`` < ``internal`` < ``egress``). The provider maps each tier
# onto its runtime's network mode. ``none`` is the model default — an egress-open
# default is a SETTINGS choice at the consumer, not a property of the shape.
SandboxNetwork = Literal["none", "internal", "egress"]

# The isolation tier a session runs under, ordered strength weakest→strongest
# (``none`` < ``container`` < ``vm``): ``none`` = no isolation (code runs directly
# on the host), ``container`` = OS/namespace-level isolation, ``vm`` =
# virtualization/kernel-level isolation. The provider maps each tier onto its
# runtime or REJECTS the ones it cannot satisfy; the vocabulary stays neutral,
# exactly as ``SandboxNetwork`` does.
SandboxIsolation = Literal["none", "container", "vm"]

# The durability tier of a session's workspace. ``persistent`` binds the session
# to a durable workspace volume named from ``workspace_key`` that survives the
# session and its reap; ``ephemeral`` gives a scratch workspace that dies with
# the session.
SandboxDurability = Literal["ephemeral", "persistent"]

# A workspace key is embedded into provider resource names, so it is charset- and
# length-constrained to what those names admit.
WORKSPACE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SandboxSessionSpec(BaseModel):
    """The requested shape of one sandbox session.

    The CONSUMER declares the session it needs; the provider maps each field onto
    its runtime or REJECTS with :class:`~tai42_contract.sandbox.SandboxSpecRejectedError`
    what it cannot honor — it never silently downgrades a request.
    """

    # The exact runnable image reference; the consumer pins it (a digest, never a
    # bare tag, is the consumer's rule).
    image: str

    # Stable identity of the workspace, charset-validated so a provider can embed
    # it in resource names (e.g. a durable volume ``tai-sbx-<workspace_key>``).
    workspace_key: str

    # ``persistent`` binds the session to a durable workspace volume that survives
    # the session and its reap; ``ephemeral`` gives a scratch workspace that dies
    # with the session. The provider maps the tier onto its runtime or REJECTS if
    # it cannot honor ``persistent`` — never silently downgrades.
    durability: SandboxDurability

    # The ONLY credential channel into the session: every value is a secret, never
    # logged, injected into a CLEAN env (never the host env). It is the BASE
    # environment of EVERY ``exec`` / ``exec_start`` subprocess in the session —
    # the provider merges it with any per-exec ``env=`` overlay, per-exec keys
    # overriding on collision.
    env: dict[str, SecretStr] = Field(default_factory=dict)

    network: SandboxNetwork = "none"

    # The isolation level the session must run at. ``None`` INHERITS the operator
    # policy's isolation floor at the kit create seam; a set value REQUESTS
    # at-least-that level, validated against the floor there. The provider maps the
    # effective level onto its runtime or REJECTS if it cannot reach it.
    isolation: SandboxIsolation | None = None

    # Resource caps; the provider enforces them or REJECTS if it cannot — never
    # silently ignores.
    cpu: float | None = None
    memory_mb: int | None = None

    # The reap deadline for an idle session, in seconds; must be positive.
    ttl_seconds: int = Field(gt=0)

    # Consumer bookkeeping; providers must round-trip these.
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("workspace_key")
    @classmethod
    def _check_workspace_key(cls, value: str) -> str:
        if not WORKSPACE_KEY_RE.fullmatch(value):
            raise ValueError(f"workspace_key must match {WORKSPACE_KEY_RE.pattern}: {value!r}")
        return value


class ExecResult(BaseModel):
    """The outcome of a completed non-interactive ``exec``."""

    exit_code: int
    stdout: str
    stderr: str


class SandboxSessionInfo(BaseModel):
    """The observable state of a live session, returned by ``info()`` / ``list_sessions()``."""

    id: str
    workspace_key: str
    # The provider's ABSOLUTE root path for this session's workspace — the same
    # value the session exposes as its ``workspace_path`` property, so a caller can
    # read the root off ``info()`` too.
    workspace_path: str
    durability: SandboxDurability
    created_at: datetime
    expires_at: datetime
    labels: dict[str, str] = Field(default_factory=dict)


class SandboxStreamChunk(BaseModel):
    """One interleaved output frame from an interactive ``exec_start``."""

    stream: Literal["stdout", "stderr"]
    data: bytes


class SandboxStreamExit(BaseModel):
    """The interactive iterator's final item, carrying the exec's exit code."""

    exit_code: int
