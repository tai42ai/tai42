"""The route entry-gate admission ladder shared by the page door and the rotate door."""

from __future__ import annotations

import logging

from starlette.requests import Request

from tai42_channel_web.routes.session_access import _client_bucket
from tai42_channel_web.store.entry_gate import check_entry_code, entry_attempt_allowed, is_gate_enabled

logger = logging.getLogger(__name__)

# The route is gated and the navigation's code is missing, unknown, expired, revoked,
# or the client is throttled — ONE code and ONE wording for all five, so no response
# ever differs by code validity (no oracle). The message is shared by the page door's
# refusal page and the rotate door's JSON refusal.
_ENTRY_REFUSED_CODE = "entry_refused"
_ENTRY_REFUSED_MESSAGE = "This chat is private. Open it from the link you were given."


async def _entry_gate_outcome(request: Request, identity: str, entry_code: str | None) -> str | None:
    """The entry-gate admission ladder for a mint-needing caller on ``identity``: the
    refusal reason (``missing``/``throttled``/``unknown``) or ``None`` when the route
    is ungated or the code is live.

    The throttle is checked BEFORE the code lookup; missing/throttled/unknown all
    resolve to a refusal, so no response ever differs by code validity (no oracle).
    The presented value is never returned or logged. Both the page door and the rotate
    door call this one ladder and map a non-``None`` outcome to their own refusal
    shape — an HTML page or a JSON envelope."""
    if not await is_gate_enabled(identity):
        return None
    bucket = _client_bucket(request)
    if entry_code is None:
        outcome = "missing"
    elif not await entry_attempt_allowed(bucket):
        outcome = "throttled"
    elif not await check_entry_code(identity, entry_code):
        outcome = "unknown"
    else:
        return None
    logger.warning("web chat refused entry for identity %s: %s (bucket %s)", identity, outcome, bucket)
    return outcome
