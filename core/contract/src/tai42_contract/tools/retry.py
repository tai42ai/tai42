"""The per-tool retry-policy declaration (``@app.tools.tool(retry=...)``).

A tool OPTS IN to automatic retry of transient failures at the host's shared
dispatch seam by declaring a :class:`ToolRetryPolicy` at registration. No
declaration means exactly one attempt — today's behavior, untouched; there is no
environment-level default policy, so the seam retries nothing a tool did not
explicitly declare.

THE DOUBLE-SEND GUARD
---------------------
A retry re-fires the tool BODY, so a non-idempotent tool (one whose repeat has a
side effect a caller cannot distinguish from the first — a send, a charge, a
create) must never be blanket-retried. ``idempotent`` is therefore REQUIRED (the
author must state the claim, never inherit a default) and a policy asking for
more than one attempt with ``idempotent=False`` is rejected at declaration —
the guard is structural, not a runtime honor-system.

COMPOSITION WITH A TOOL'S OWN INTERNAL RETRY
--------------------------------------------
Some tool bodies already loop attempts internally (the channel delivery seams
retry their sends against their own budget). The seam composes with those by
DECLARATION OWNERSHIP: a tool with an internal loop simply declares no policy
and the seam adds zero attempts; a tool that declares one is explicitly
asserting the outer attempts on top of whatever its body does internally — the
multiplication is the declarer's, never the platform's.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tai42_contract.errors import ErrorKind

#: The error kinds a policy with ``retryable=True`` treats as transient: a
#: timeout or an unreachable dependency may pass on a fresh attempt. Everything
#: else — bad input, a permission denial, a not-found, an UNKNOWN — is either
#: deterministic or unclassified, and an unclassified failure is never retried.
DEFAULT_RETRYABLE_KINDS: frozenset[ErrorKind] = frozenset({ErrorKind.TIMED_OUT, ErrorKind.UNAVAILABLE})

#: Kinds a policy may never declare retryable: an UNKNOWN failure is one nothing
#: classified (blind-retrying it is the exact hazard the allowlist exists to
#: prevent), and a cancellation is a control-flow signal, not a failure.
NEVER_RETRYABLE_KINDS: frozenset[ErrorKind] = frozenset({ErrorKind.UNKNOWN, ErrorKind.CANCELLED})

#: Hard ceiling on ``max_attempts`` — bounded by construction, so a declaration
#: can never spin a dispatch into an effectively unbounded loop.
MAX_ATTEMPTS_CEILING = 10


class ToolRetryBackoff(BaseModel):
    """The exponential-backoff shape between attempts: attempt ``n`` waits
    ``min(cap_seconds, initial_seconds * multiplier**(n-1))`` before attempt
    ``n+1``. A server-provided ``retry_after`` on the failed attempt's error
    WIDENS the wait when it asks for longer (the medium's own ask wins, even
    past the cap — the cap bounds the platform's growth, not the server's
    explicit request)."""

    model_config = ConfigDict(frozen=True)

    initial_seconds: float = Field(default=1.0, gt=0)
    multiplier: float = Field(default=2.0, ge=1.0)
    cap_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _cap_covers_initial(self) -> ToolRetryBackoff:
        if self.cap_seconds < self.initial_seconds:
            raise ValueError("cap_seconds must be at least initial_seconds")
        return self


class ToolRetryPolicy(BaseModel):
    """A tool's declared retry policy, consumed by the host dispatch seam.

    ``max_attempts`` is the TOTAL attempt budget, first attempt included
    (``1`` = no retry, just per-attempt monitoring). ``idempotent`` is the
    author's explicit claim that re-firing the body is safe — REQUIRED, and a
    retrying policy (``max_attempts > 1``) without it is rejected (the
    double-send guard).

    ``retryable`` is the classification door — an explicit allowlist, never a
    blanket:

    * ``True`` — retry the :data:`DEFAULT_RETRYABLE_KINDS`;
    * a tuple of :class:`ErrorKind` — retry exactly those kinds
      (:data:`NEVER_RETRYABLE_KINDS` are rejected at declaration);
    * ``False`` — no kind-based retry at all.

    In every mode, an error carrying its OWN boolean ``retryable`` verdict (the
    ``ChannelDeliveryError`` shape) wins in BOTH directions: ``True`` admits it
    even off-list, ``False`` vetoes it even on-list — the raiser knows its
    failure better than any kind bucket. An error with neither a verdict nor an
    allowlisted kind is never retried.
    """

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(ge=1, le=MAX_ATTEMPTS_CEILING)
    backoff: ToolRetryBackoff = ToolRetryBackoff()
    retryable: bool | tuple[ErrorKind, ...] = True
    idempotent: bool

    @model_validator(mode="after")
    def _guards(self) -> ToolRetryPolicy:
        if self.max_attempts > 1 and not self.idempotent:
            raise ValueError(
                "a retrying policy (max_attempts > 1) requires idempotent=True — "
                "a non-idempotent tool must not be re-fired (the double-send guard)"
            )
        if isinstance(self.retryable, tuple):
            if not self.retryable:
                raise ValueError("retryable kinds must be non-empty; use retryable=False to disable kind-based retry")
            forbidden = NEVER_RETRYABLE_KINDS.intersection(self.retryable)
            if forbidden:
                raise ValueError(f"retryable kinds may never include: {', '.join(sorted(k.value for k in forbidden))}")
        return self


__all__ = [
    "DEFAULT_RETRYABLE_KINDS",
    "MAX_ATTEMPTS_CEILING",
    "NEVER_RETRYABLE_KINDS",
    "ToolRetryBackoff",
    "ToolRetryPolicy",
]
