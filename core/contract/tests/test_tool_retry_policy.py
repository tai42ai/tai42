"""The ``ToolRetryPolicy`` declaration model: field bounds, the structural
double-send guard (a retrying policy requires an explicit ``idempotent=True``),
the classification-allowlist validation (never UNKNOWN/CANCELLED), and the
backoff shape guards."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.errors import ErrorKind
from tai42_contract.tools import (
    DEFAULT_RETRYABLE_KINDS,
    MAX_ATTEMPTS_CEILING,
    NEVER_RETRYABLE_KINDS,
    ToolRetryBackoff,
    ToolRetryPolicy,
)


def test_minimal_retrying_policy_defaults():
    policy = ToolRetryPolicy(max_attempts=3, idempotent=True)
    assert policy.max_attempts == 3
    assert policy.retryable is True
    assert policy.backoff.initial_seconds == 1.0
    assert policy.backoff.multiplier == 2.0
    assert policy.backoff.cap_seconds == 30.0


def test_idempotent_is_required():
    # The author must STATE the idempotency claim — there is no default to inherit.
    with pytest.raises(ValidationError):
        ToolRetryPolicy.model_validate({"max_attempts": 3})


def test_retrying_policy_rejects_non_idempotent():
    # The double-send guard is structural: a policy that could re-fire a
    # non-idempotent body never validates, so the runtime loop never sees one.
    with pytest.raises(ValidationError, match="double-send"):
        ToolRetryPolicy(max_attempts=2, idempotent=False)


def test_single_attempt_policy_may_be_non_idempotent():
    # max_attempts=1 never re-fires, so a non-idempotent tool may still opt into
    # the per-attempt monitoring the policy arms.
    policy = ToolRetryPolicy(max_attempts=1, idempotent=False)
    assert policy.max_attempts == 1


def test_max_attempts_bounds():
    with pytest.raises(ValidationError):
        ToolRetryPolicy(max_attempts=0, idempotent=True)
    with pytest.raises(ValidationError):
        ToolRetryPolicy(max_attempts=MAX_ATTEMPTS_CEILING + 1, idempotent=True)
    assert ToolRetryPolicy(max_attempts=MAX_ATTEMPTS_CEILING, idempotent=True).max_attempts == MAX_ATTEMPTS_CEILING


def test_declared_kinds_accepted_and_coerced():
    policy = ToolRetryPolicy.model_validate(
        {"max_attempts": 3, "idempotent": True, "retryable": ["timed_out", "upstream_error"]}
    )
    assert policy.retryable == (ErrorKind.TIMED_OUT, ErrorKind.UPSTREAM_ERROR)


@pytest.mark.parametrize("forbidden", sorted(NEVER_RETRYABLE_KINDS))
def test_declared_kinds_reject_never_retryable(forbidden: ErrorKind):
    # UNKNOWN is unclassified (blind-retrying it is the exact hazard the
    # allowlist prevents); CANCELLED is control flow, not a failure.
    with pytest.raises(ValidationError, match="never include"):
        ToolRetryPolicy(max_attempts=3, idempotent=True, retryable=(forbidden,))


def test_declared_kinds_reject_empty_tuple():
    with pytest.raises(ValidationError, match="non-empty"):
        ToolRetryPolicy(max_attempts=3, idempotent=True, retryable=())


def test_default_transient_set_shape():
    # The retryable=True meaning is contract-pinned: transient reach/timing
    # failures only, never UNKNOWN and never the deterministic kinds.
    assert frozenset({ErrorKind.TIMED_OUT, ErrorKind.UNAVAILABLE}) == DEFAULT_RETRYABLE_KINDS
    assert not DEFAULT_RETRYABLE_KINDS & NEVER_RETRYABLE_KINDS


def test_backoff_shape_guards():
    with pytest.raises(ValidationError):
        ToolRetryBackoff(initial_seconds=0)
    with pytest.raises(ValidationError):
        ToolRetryBackoff(multiplier=0.5)
    with pytest.raises(ValidationError, match="at least initial_seconds"):
        ToolRetryBackoff(initial_seconds=5.0, cap_seconds=1.0)


def test_policy_is_frozen():
    policy = ToolRetryPolicy(max_attempts=3, idempotent=True)
    with pytest.raises(ValidationError):
        policy.max_attempts = 5  # type: ignore[misc]
