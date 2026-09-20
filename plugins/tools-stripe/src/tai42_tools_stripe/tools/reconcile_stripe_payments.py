"""The ``reconcile_stripe_payments`` tool: the recovery layer that re-answers lost payments.

Re-derives the truth from Stripe's own Checkout Session list -- the system of record for whether
money moved -- and re-answers anything the single-delivery webhook path lost, for as long as the
ask is alive. The Stripe list call, the livemode assert, the SSRF pin, the answer builder and the
bounded retry live in :mod:`tai42_tools_stripe._internal.tools.stripe_client`.

This is NOT a user or agent tool: it holds the bridge secret and answers payment asks. Its callers
are the scheduler and an authed operator.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_stripe._internal.tools.stripe_client import (
    CallbackDoorError,
    _assert_livemode,
    build_answer_payload,
    list_checkout_sessions,
    post_answer,
    stripe_settings,
)

# Indirection point so tests can observe the inter-answer pacing without waiting; nothing else
# reads it. The pace VALUE is the settings field, never a module constant.
_sleep = asyncio.sleep


def _selected_callback_url(session: dict[str, Any]) -> str | None:
    """The session's ``tai_callback_url`` when it is a paid session carrying one, else ``None``.

    Unpaid sessions and paid sessions with no callback url are skipped, not counted.
    """
    if session.get("payment_status") != "paid":
        return None
    metadata = session.get("metadata") or {}
    return metadata.get("tai_callback_url") or None


async def _answer_session(session: dict[str, Any], callback_url: str) -> tuple[str, dict[str, Any] | None]:
    """Attempt to answer one paid session.

    Returns an outcome key (``answered``/``already_answered``/``expired``/``rejected``/``failed``)
    and, for ``failed`` only, the ``{session_id, error}`` record.

    A per-session verdict (door 404 → ``expired``, door 400 → ``rejected``) is counted,
    never raised. Deployment-wide breakage (door 403, livemode mismatch, SSRF refusal,
    transport error, retry exhaustion, unrecognised 200 body) becomes ``failed`` for the
    caller's terminal raise, so one unanswerable session never aborts the rest.
    """
    session_id = session.get("id")
    try:
        _assert_livemode(session)
        body = await post_answer(callback_url, build_answer_payload(session))
    except CallbackDoorError as exc:
        if exc.status == 404:
            return "expired", None
        if exc.status == 400:
            return "rejected", None
        return "failed", {"session_id": session_id, "error": str(exc)}
    except Exception as exc:
        # Batch collector: any non-CallbackDoorError (livemode mismatch, SSRF refusal,
        # transport error, retry exhaustion) is recorded and surfaced by the caller's raise.
        return "failed", {"session_id": session_id, "error": str(exc)}

    status = (body.get("data") or {}).get("status")
    if status == "answered":
        return "answered", None
    if status == "already_answered":
        return "already_answered", None
    return "failed", {"session_id": session_id, "error": f"unrecognised door body: {body!r}"}


@tai42_app.tools.tool(tags={"stripe", "payments"})
async def reconcile_stripe_payments(lookback_hours: int = 26) -> dict[str, Any]:
    """Re-answer every paid Checkout Session in the lookback window the webhook path may have lost.

    Returns a per-outcome summary.

    ``lookback_hours`` is bounded ``1..168`` and raises outside it before touching Stripe: below 1
    is a no-op dressed as a run, above one week walks into the list ceiling. The default 26 covers
    a Checkout link's full ~24h lifetime plus slack. The recovery guarantee is the LOOKBACK, not
    the ask's lifetime -- an outage longer than the window needs a manual wide-window run.

    Sessions are answered SERIALLY and PACED by ``STRIPE_RECONCILE_ANSWER_INTERVAL_SECONDS``
    (default 1.2s) to stay off the callback door's rate limiter. Outcomes split on per-session vs
    deployment-wide: a door 404 is ``expired`` and a door 400 is ``rejected`` -- verdicts on one
    session that are counted, reported and never raised. A door 403, a livemode mismatch, an
    SSRF-pin refusal, a transport error, retry exhaustion and an unrecognised 200 body are all
    deployment-wide breakage arriving one session at a time: they land in ``failed`` and the run
    RAISES at the end, after every other session has been attempted. Skipped sessions (unpaid, or
    with no ``tai_callback_url``) are not this tool's business and are not counted.

    Args:
        lookback_hours: How far back to list sessions, in hours. Must be in ``1..168``.

    Returns:
        A summary ``{selected, answered, already_answered, expired, rejected, failed}`` where
        ``selected == answered + already_answered + expired + rejected + len(failed)``.
    """
    if not 1 <= lookback_hours <= 168:
        raise ValueError(f"lookback_hours must be in 1..168; got {lookback_hours}")

    created_gte = int(time.time()) - lookback_hours * 3600
    sessions = await list_checkout_sessions(created_gte)
    interval = stripe_settings().reconcile_answer_interval_seconds

    counts = {"answered": 0, "already_answered": 0, "expired": 0, "rejected": 0}
    failed: list[dict[str, Any]] = []
    selected = 0

    for session in sessions:
        callback_url = _selected_callback_url(session)
        if callback_url is None:
            continue

        if selected and interval > 0:
            await _sleep(interval)
        selected += 1

        outcome, error = await _answer_session(session, callback_url)
        if error is not None:
            failed.append(error)
        else:
            counts[outcome] += 1

    summary = {"selected": selected, **counts, "failed": failed}
    if failed:
        raise ValueError(f"reconciliation left {len(failed)} session(s) failed: {summary}")
    return summary
