"""Inbound WhatsApp webhook — where the human's reply and delivery receipts enter the system.

``/api/channels/whatsapp/inbound`` is unauthenticated (Meta cannot send the
platform api key):

* ``GET`` is Meta's subscription handshake — echo ``hub.challenge`` in plaintext
  IFF ``hub.verify_token`` matches the configured token (constant-time), else 403.
* ``POST`` carries message and delivery-status events, signed with
  ``X-Hub-Signature-256`` = ``sha256=<hex>`` HMAC-SHA256 over the RAW body,
  validated fail-closed BEFORE the body is parsed. A missing configured app
  secret is a loud misconfiguration (logged 500), never a skipped check.

Meta's signature scheme carries no timestamp, so the ``wamid`` dedupe is the
replay guard. A reply matching a pending question is forwarded as ``{"answer":
<value>}`` — the text verbatim (outer whitespace stripped) for a text reply, the
resolved option for an interactive tap, or the schema-coerced dict for a
completed Flow form (``nfm_reply``); on a correlation miss the message enters the
conversation bridge instead. An ``nfm_reply`` whose flow token rides the
``tai42-nf:`` namespace is an ASK-LESS form (a ``notify`` Flow): it has no
reservation and enters the bridge as a structured participant message, routed by its
token prefix before any pending-question lookup.

Importing this package registers the webhook route as a side-effect (via
``.webhook``); the modules below hold the door authentication, payload traversal,
message routing, and reply/status handling.
"""

from tai42_channel_whatsapp.inbound import webhook  # noqa: F401  (route registration side-effect)
