"""HTTP surface for the internal notifications sink and the send door.

- ``GET /api/notifications`` (AUTHED) — return the deployment's internal
  notifications, newest-first. Each record is ``{"id", "message", "recipient",
  "audience", "media", "template", "options", "schema", "created_at"}`` — the rich
  fields
  hold the richer-send forms the notification carried (``None`` each for a plain
  text record; ``schema`` only via an audience-addressed channel form send). A
  restricted caller
  reads its own per-identity feed (only its ``audience``-addressed records); an
  unrestricted caller reads the shared feed.
- ``POST /api/notifications`` (AUTHED) — send a human a one-way, fire-and-forget
  notification: on a named ``channel``, or (channel omitted) recorded to the
  internal sink, optionally carrying the richer-send forms ``media`` (blank
  ``message`` allowed for a media-only send) / ``template`` / ``options`` /
  ``schema`` (an ask-less form: the message is the form's prompt, and a submission
  enters the conversation as a message from the person; channel-only — the sink
  has no submission door). A blank
  message with no media / unknown channel / blank recipient / blank audience — a
  rich combination the contract refuses, or a schema with no channel or outside the
  channel's renderable form subset — is a loud 400, a channel that cannot
  notify (or does not advertise a needed rich capability, form included) a 501, and
  a delivery
  failure a 502.

Both doors are thin adapters over operations in
``tai42_skeleton.operations.notifications``. Success bodies are ``{"data": ...}``;
failures are ``{"error": "<message>"}``.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.operations import operation_metadata_of, register_operation_route
from tai42_skeleton.operations.notifications import list_notifications as _list_notifications_op
from tai42_skeleton.operations.notifications import notify_user as _notify_user_op

list_notifications = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_notifications_op),
    path="/api/notifications",
    method="GET",
    action="read",
)

notify_user = register_operation_route(
    tai42_app,
    operation_metadata_of(_notify_user_op),
    path="/api/notifications",
    method="POST",
    action="write",
)
