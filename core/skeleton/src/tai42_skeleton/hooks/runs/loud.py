"""The loud path of a hook run held as failed: a log, the operator notification, the platform event.

A failed run is held until an operator retries or discards it, so its failure is surfaced on every
channel a deployment watches.
"""

from __future__ import annotations

import logging
from typing import Any

from .metrics import hook_run_metrics
from .models import HookRun

logger = logging.getLogger(__name__)

# The platform-event topic emitted when a hook run is held as failed; a deployment wires its own hook on it.
HOOK_RUN_FAILED_EVENT_TOPIC = "hooks_run_failed"


async def report_failed(run: HookRun) -> None:
    """Report ``run``, now failed: ERROR log, operator notification, the platform event.

    The event is itself kept as hook runs, accepted with the run's own ``monitoring_disabled``. A
    failed run whose own topic is the failed-run topic sends no further event, so a failing hook
    on that topic never feeds itself.
    """
    hook_run_metrics().failed.inc()
    error = run.last_error or ""
    acted_ids = list(run.acted_ids)
    intent = "" if run.claim_intent is None else f" claim_intent={run.claim_intent.to_json()}"
    logger.error(
        "hook run %s (%s on %s) failed after %d attempt(s) [%s], acted=%s %s: %s%s",
        run.id,
        run.hook,
        run.topic,
        run.attempts,
        run.last_error_kind,
        run.acted,
        acted_ids,
        error,
        intent,
    )
    await notify_operators(
        f"A hook run failed and is held until it is retried or discarded. Run {run.id}, hook {run.hook} "
        f"on {run.topic}, {run.attempts} attempt(s), acted={run.acted or '-'}: {error}",
        run_id=run.id,
    )
    if run.topic == HOOK_RUN_FAILED_EVENT_TOPIC:
        return
    payload: dict[str, Any] = {
        "run_id": run.id,
        "topic": run.topic,
        "hook": run.hook,
        "attempts": run.attempts,
        "acted": run.acted,
        "acted_ids": acted_ids,
        "error_kind": run.last_error_kind,
        "error": error,
    }
    from . import runtime

    try:
        await runtime.manager().on_event(
            topic=HOOK_RUN_FAILED_EVENT_TOPIC, payload=payload, monitoring_disabled=run.monitoring_disabled
        )
    except Exception:
        logger.exception("hook run %s: the %r event could not be kept", run.id, HOOK_RUN_FAILED_EVENT_TOPIC)


async def notify_operators(message: str, *, run_id: str) -> None:
    """Write ``message`` to the operator notification feed; a feed that cannot take it is logged and counted."""
    from tai42_skeleton.channels.notifications_sink import record_notification
    from tai42_skeleton.interactions.settings import interactions_store_configured

    if not interactions_store_configured():
        logger.error("hook runs: no notification feed is configured to tell operators about failed hook run %s", run_id)
        hook_run_metrics().notify_failures.inc()
        return
    try:
        await record_notification(message)
    except Exception:
        logger.exception("hook runs: the operator notification for failed hook run %s could not be written", run_id)
        hook_run_metrics().notify_failures.inc()
