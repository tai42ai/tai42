"""The tool-retry seam engine: ``dispatch_with_retry`` + ``ToolRetryRegistry``.

Covers the no-policy passthrough (single attempt, zero monitoring — the
byte-identical guarantee at this seam), the classification door (explicit
``retryable`` verdict beats the kind allowlist in both directions; UNKNOWN is
never retried), the attempts cap (the LAST error propagates), the backoff
sequence with its cap, the server ``retry_after`` widening, the non-idempotent
runtime belt behind the model's structural guard, and the per-attempt span
stamping (``retry.attempt``, the send-span precedent). All waits go through the
module's injected sleep — no wall-clock sleeps here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tai42_contract.channels import ChannelDeliveryError
from tai42_contract.errors import ErrorKind
from tai42_contract.monitoring import MonitoringLevel, SpanKind
from tai42_contract.tools import ToolRetryBackoff, ToolRetryPolicy

import tai42_skeleton.tools.retry as retry_module
from tai42_skeleton.monitoring import init_monitoring, reset_monitoring
from tai42_skeleton.tools.retry import ToolRetryRegistry, dispatch_with_retry

from .._fakes.recording_monitoring import RecordingMonitoring


class _UpstreamBlip(Exception):
    """A typed transient upstream fault — kind-classified, no explicit verdict."""

    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Capture every backoff wait the loop would take, without sleeping."""
    recorded: list[float] = []

    async def _capture(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(retry_module, "_sleep", _capture)
    return recorded


@pytest.fixture
def backend() -> Iterator[RecordingMonitoring]:
    reset_monitoring()
    backend = RecordingMonitoring()
    init_monitoring(backend)
    yield backend
    reset_monitoring()


def _failing(times: int, error_factory, result: str = "ok"):
    """An attempt callable failing ``times`` times before returning ``result``,
    plus its call counter."""
    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        if calls["n"] <= times:
            raise error_factory()
        return result

    return attempt, calls


def _policy(**overrides) -> ToolRetryPolicy:
    fields = {
        "max_attempts": 3,
        "idempotent": True,
        "backoff": ToolRetryBackoff(initial_seconds=0.5, multiplier=2.0, cap_seconds=30.0),
    }
    fields.update(overrides)
    return ToolRetryPolicy.model_validate(fields)


# -- registry -----------------------------------------------------------------


def test_registry_register_get_reset():
    registry = ToolRetryRegistry()
    policy = _policy()
    registry.register("fetch_page", policy)
    assert registry.get("fetch_page") is policy
    assert registry.get("other") is None
    registry.reset()
    assert registry.get("fetch_page") is None


def test_registry_duplicate_registration_raises():
    registry = ToolRetryRegistry()
    registry.register("fetch_page", _policy())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("fetch_page", _policy())


# -- no policy: exactly today's behavior --------------------------------------


async def test_no_policy_is_a_single_untouched_attempt(sleeps, backend):
    # Even inside an active trace, a policy-less dispatch opens NO span, takes
    # NO sleep, and runs the attempt exactly once — byte-identical to a direct
    # await of the attempt.
    backend.writer.active_trace_id = "trace-1"
    attempt, calls = _failing(0, lambda: ChannelDeliveryError("x", retryable=True))

    assert await dispatch_with_retry("plain", None, attempt) == "ok"
    assert calls["n"] == 1
    assert sleeps == []
    assert backend.writer.spans == []


async def test_no_policy_failure_propagates_unretried(sleeps):
    attempt, calls = _failing(5, lambda: ChannelDeliveryError("x", retryable=True))

    with pytest.raises(ChannelDeliveryError):
        await dispatch_with_retry("plain", None, attempt)
    assert calls["n"] == 1
    assert sleeps == []


# -- classification: the explicit verdict and the kind allowlist ---------------


async def test_explicit_retryable_verdict_retries_to_success(sleeps):
    attempt, calls = _failing(2, lambda: ChannelDeliveryError("medium 503", retryable=True))

    assert await dispatch_with_retry("fetch", _policy(), attempt) == "ok"
    assert calls["n"] == 3
    # Exponential off the declared base: 0.5 then 1.0.
    assert sleeps == [0.5, 1.0]


async def test_unknown_error_is_never_retried(sleeps):
    # RuntimeError classifies to ErrorKind.UNKNOWN — nothing vouched for it, so
    # the loop refuses even under the widest boolean declaration.
    attempt, calls = _failing(2, lambda: RuntimeError("mystery"))

    with pytest.raises(RuntimeError):
        await dispatch_with_retry("fetch", _policy(retryable=True), attempt)
    assert calls["n"] == 1
    assert sleeps == []


async def test_default_transient_kinds_cover_timeouts(sleeps):
    attempt, calls = _failing(1, lambda: TimeoutError("slow upstream"))

    assert await dispatch_with_retry("fetch", _policy(), attempt) == "ok"
    assert calls["n"] == 2


async def test_declared_kind_allowlist_admits_only_its_kinds(sleeps):
    policy = _policy(retryable=(ErrorKind.UPSTREAM_ERROR,))
    attempt, calls = _failing(1, _UpstreamBlip)
    assert await dispatch_with_retry("fetch", policy, attempt) == "ok"
    assert calls["n"] == 2

    # A timeout is transient by default — but this policy did not declare it.
    attempt, calls = _failing(1, lambda: TimeoutError("slow"))
    with pytest.raises(TimeoutError):
        await dispatch_with_retry("fetch", policy, attempt)
    assert calls["n"] == 1


async def test_explicit_false_verdict_vetoes_a_listed_kind(sleeps):
    # ChannelDeliveryError is DELIVERY_FAILED-kind; even declared retryable by
    # kind, the raiser's own retryable=False verdict wins.
    policy = _policy(retryable=(ErrorKind.DELIVERY_FAILED,))
    attempt, calls = _failing(1, lambda: ChannelDeliveryError("bad recipient", retryable=False))

    with pytest.raises(ChannelDeliveryError):
        await dispatch_with_retry("notify", policy, attempt)
    assert calls["n"] == 1
    assert sleeps == []


async def test_retryable_false_mode_honors_only_the_explicit_verdict(sleeps):
    policy = _policy(retryable=False)

    # No kind-based retry at all — a timeout fails on the first try.
    attempt, calls = _failing(1, lambda: TimeoutError("slow"))
    with pytest.raises(TimeoutError):
        await dispatch_with_retry("fetch", policy, attempt)
    assert calls["n"] == 1

    # ...but an error explicitly vouching retryable=True still retries.
    attempt, calls = _failing(1, lambda: ChannelDeliveryError("503", retryable=True))
    assert await dispatch_with_retry("fetch", policy, attempt) == "ok"
    assert calls["n"] == 2


async def test_cancellation_propagates_unretried(sleeps):
    import asyncio

    attempt, calls = _failing(1, lambda: asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await dispatch_with_retry("fetch", _policy(), attempt)
    assert calls["n"] == 1
    assert sleeps == []


# -- attempts cap, backoff, retry_after ---------------------------------------


async def test_exhaustion_propagates_the_last_error(sleeps):
    errors = [ChannelDeliveryError(f"failure {n}", retryable=True) for n in range(3)]
    calls = {"n": 0}

    async def attempt():
        error = errors[calls["n"]]
        calls["n"] += 1
        raise error

    with pytest.raises(ChannelDeliveryError) as excinfo:
        await dispatch_with_retry("fetch", _policy(max_attempts=3), attempt)
    # The LAST attempt's error — honest, never the first one replayed.
    assert excinfo.value is errors[2]
    assert calls["n"] == 3
    assert len(sleeps) == 2


async def test_backoff_sequence_respects_the_cap(sleeps):
    policy = _policy(
        max_attempts=6,
        backoff=ToolRetryBackoff(initial_seconds=1.0, multiplier=2.0, cap_seconds=5.0),
    )
    attempt, _calls = _failing(5, lambda: TimeoutError("slow"))

    assert await dispatch_with_retry("fetch", policy, attempt) == "ok"
    assert sleeps == [1.0, 2.0, 4.0, 5.0, 5.0]


async def test_retry_after_widens_the_wait(sleeps):
    # The medium asked for 7.5s; backoff would only wait 0.5s — the server's
    # explicit ask wins, even past the cap.
    policy = _policy(backoff=ToolRetryBackoff(initial_seconds=0.5, multiplier=2.0, cap_seconds=2.0))
    attempt, _calls = _failing(1, lambda: ChannelDeliveryError("429", retryable=True, retry_after=7.5))

    assert await dispatch_with_retry("fetch", policy, attempt) == "ok"
    assert sleeps == [7.5]


async def test_retry_after_never_narrows_the_wait(sleeps):
    # A retry_after SHORTER than the computed backoff does not shrink it — the
    # wait only ever widens (the delivery-loop precedent).
    policy = _policy(backoff=ToolRetryBackoff(initial_seconds=3.0, multiplier=2.0, cap_seconds=30.0))
    attempt, _calls = _failing(1, lambda: ChannelDeliveryError("429", retryable=True, retry_after=0.25))

    assert await dispatch_with_retry("fetch", policy, attempt) == "ok"
    assert sleeps == [3.0]


async def test_retry_after_below_ceiling_is_honored_exactly(sleeps):
    # Just under the ceiling: the server's ask is honored to the second — the
    # cap only ever engages on a runaway value.
    policy = _policy(backoff=ToolRetryBackoff(initial_seconds=0.5, multiplier=2.0, cap_seconds=2.0))
    ask = retry_module._MAX_RETRY_AFTER_SECONDS - 1.0
    attempt, _calls = _failing(1, lambda: ChannelDeliveryError("429", retryable=True, retry_after=ask))

    assert await dispatch_with_retry("fetch", policy, attempt) == "ok"
    assert sleeps == [ask]


async def test_retry_after_above_ceiling_waits_only_the_ceiling(sleeps):
    # An hour-scale Retry-After from a downstream must not park a detached
    # dispatch: the honored wait is bounded to the ceiling, never the raw ask.
    policy = _policy(backoff=ToolRetryBackoff(initial_seconds=0.5, multiplier=2.0, cap_seconds=2.0))
    attempt, _calls = _failing(1, lambda: ChannelDeliveryError("429", retryable=True, retry_after=3600.0))

    assert await dispatch_with_retry("fetch", policy, attempt) == "ok"
    assert sleeps == [retry_module._MAX_RETRY_AFTER_SECONDS]


# -- the non-idempotent runtime belt ------------------------------------------


async def test_non_idempotent_policy_never_retries_at_runtime(sleeps):
    # The model rejects this shape at declaration; if one is ever materialized
    # past validation, the loop itself still refuses to re-fire the body.
    policy = ToolRetryPolicy.model_construct(
        max_attempts=3, backoff=ToolRetryBackoff(), retryable=True, idempotent=False
    )
    attempt, calls = _failing(2, lambda: ChannelDeliveryError("503", retryable=True))

    with pytest.raises(ChannelDeliveryError):
        await dispatch_with_retry("send_once", policy, attempt)
    assert calls["n"] == 1
    assert sleeps == []


# -- monitoring: per-attempt visibility ---------------------------------------


async def test_attempt_spans_stamp_the_retry_ordinal(sleeps, backend):
    backend.writer.active_trace_id = "trace-1"
    attempt, _calls = _failing(2, lambda: ChannelDeliveryError("503", retryable=True, retry_after=2.0))

    assert await dispatch_with_retry("fetch", _policy(), attempt) == "ok"

    assert [s["name"] for s in backend.writer.spans] == ["tool-attempt:fetch"] * 3
    assert all(s["kind"] is SpanKind.TOOL for s in backend.writer.spans)
    assert [s["metadata"] for s in backend.writer.spans] == [
        {"retry.attempt": 1, "retry.max_attempts": 3},
        {"retry.attempt": 2, "retry.max_attempts": 3},
        {"retry.attempt": 3, "retry.max_attempts": 3},
    ]
    # Failed attempts are ERROR-marked with the typed failure detail; the
    # succeeding attempt's span carries no update.
    first, second, third = (s["span"].updates for s in backend.writer.spans)
    for updates in (first, second):
        (update,) = updates
        assert update["level"] is MonitoringLevel.ERROR
        assert update["metadata"] == {
            "error.type": "ChannelDeliveryError",
            "error.kind": "delivery_failed",
            "retryable": True,
            "retry_after": 2.0,
        }
    assert third == []


async def test_attempt_spans_noop_outside_a_trace(sleeps, backend):
    # The conditional-emit idiom: no ambient trace, no spans — but the retry
    # loop itself is untouched.
    backend.writer.active_trace_id = None
    attempt, calls = _failing(1, lambda: TimeoutError("slow"))

    assert await dispatch_with_retry("fetch", _policy(), attempt) == "ok"
    assert calls["n"] == 2
    assert backend.writer.spans == []
