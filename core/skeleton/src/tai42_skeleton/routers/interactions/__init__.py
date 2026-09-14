"""HTTP routes for the ask_user interactions surface — ``/api/interactions/*``.

Doors:

* ``GET /api/interactions`` — the authenticated PAGED pending-question list (the
  ``list_interactions`` operation): the initial-load surface a client reads before
  applying the live stream. Audience-filtered before paging so the totals are honest.
* ``GET /api/interactions/stream`` — the authenticated TAIL-ONLY SSE feed: a live
  tail of add/answered/removed events from the cursor captured at connect. It
  carries no historical backlog and no end-of-backlog marker (the paged list door
  is the initial-load surface).
* ``GET /api/interactions/media/{media_id}`` — the UNAUTHENTICATED served-media
  door: the media id is the capability secret (a vendor fetches the url from its
  own servers, a browser ``<img>`` from the inbox origin). Serves the stored bytes
  with their mime; a malformed id is a 400, a miss/expired id a 404.
* ``POST /api/interactions/{interaction_id}/answer`` — the authenticated human
  answer door. The value is validated server-side against the stored question's
  ``answer_format`` before the blocked caller is woken; an invalid answer is
  rejected loudly and the caller stays blocked. An EXTERNAL question is answered
  through its callback URL, never here.
* ``POST /api/interactions/{interaction_id}/cancel`` — the authenticated cancel
  door: WITHDRAW one pending ask without answering it and without deleting its
  thread. Status-gated (a pending/parked question cancels; an answered one is a
  409, a gone/expired one a 404), fires NO continuation (a parked flow never
  resumes), and emits the removed event tagged ``reason="cancelled"``. Same
  audience gate as the answer door.
* ``POST /api/interactions/callback/{ticket}`` — the UNAUTHENTICATED data door
  for external-format answers (the server-to-server / confirm-form claim path).
  Sensitive data rides the JSON body here.
* ``GET /api/interactions/callback/{ticket}`` — the UNAUTHENTICATED redirect
  door. GET never mutates state (link scanners prefetch these URLs). It serves a
  page by the question's answer format: for confirm/external the byte-constant
  confirm page whose form POSTs back to the same URL; for a channel-delivered
  ``form`` a schema-rendered HTML form that POSTs ``{"answer": {...}}`` as JSON
  to the same URL; for text/select an informational awaiting-reply page (a bare
  confirm tap carries no value answer).

Importing this package imports each door submodule, registering every route. The
door callables are re-exported here (the public path ``tai42_skeleton.routers.interactions``
is unchanged). ``client_ctx``, ``interactions_settings`` and ``_KEEPALIVE_SECONDS`` are
homed here so a test's patch at this package alias is the single point every door reads
through at call time.
"""

from __future__ import annotations

from tai42_kit.clients import client_ctx

from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured

from .answer import answer, cancel

# The callback internals a callback-door test drives directly through the package alias.
from .callback import _POST_ONLY_EMPTY_BODY_DENY as _POST_ONLY_EMPTY_BODY_DENY
from .callback import _record_callback_answer as _record_callback_answer
from .callback import callback
from .listing import list_interactions, list_pending_interactions
from .media import media

# ``_now`` re-exported (``as`` marks the explicit re-export) so a test's ``router._now``
# clock patch is the single seam the stream door reads through at call time; the tail
# generator and its connect frame are driven directly through the package alias too.
from .stream import _CONNECT_FRAME as _CONNECT_FRAME
from .stream import _now as _now
from .stream import _stream_events as _stream_events
from .stream import stream

# The SSE keepalive cadence, homed at the package alias so a test overrides it here and
# the stream door reads it through the package at call time.
_KEEPALIVE_SECONDS = 15

__all__ = [
    "answer",
    "callback",
    "cancel",
    "client_ctx",
    "interactions_settings",
    "interactions_store_configured",
    "list_interactions",
    "list_pending_interactions",
    "media",
    "stream",
]
