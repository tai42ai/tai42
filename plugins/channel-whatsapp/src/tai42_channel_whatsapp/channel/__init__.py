"""The WhatsApp channel: ``deliver`` sends one question, ``notify`` sends one fire-and-forget message.

Tier-1 formats (``confirm``, ``external``) carry the callback_url as a tappable
link and store NO correlation — the human answers via the callback door; confirm
MUST take this path (the door accepts only the link-tap bool). Tier-2 (``text``,
``select``) expect a WhatsApp reply matched through the correlation store; the
reservation is written BEFORE the send so a reply racing the send response is
still matchable, and a failed send releases the reservation and raises. A
``select`` ask renders natively where it fits — interactive reply buttons for a
few short options, an interactive list for more, and the numbered-text fallback
past those caps — and the human may always type an option instead of tapping. A
``form`` ask is also Tier-2: it renders as an in-chat WhatsApp Flow (created and
published once per answer schema, then reused), and the completed form returns as
an ``nfm_reply`` matched by the delivery's ``interaction_id`` as the flow token.
An ask-less form NOTIFICATION reuses the same Flow machinery with a namespaced
token instead of a reservation — its submission enters the conversation as a
structured visitor message, never as an answer.

Freeform sends (questions, replies, media) go to any requested recipient: Meta's
own 24-hour customer-service window is the fence (a send outside it is rejected,
error 131047, and raises). A TEMPLATE send is the one send Meta delivers cold, so
it keeps an operator fence — the recipient must be on
``CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS`` or be a known contact (a pair the inbound
webhook saw within the configured window). A recipient is always caller-supplied;
this channel has no default recipient.

The submodules below hold the protocol adapter, the ask/notification/form/media
rendering, the tappable-choice primitives, and recipient resolution.
"""

from tai42_channel_whatsapp.channel.adapter import WhatsAppChannel
from tai42_channel_whatsapp.channel.forms import _NOTIFY_FORM_TOKEN_PREFIX, send_form_ask_flow

__all__ = ["_NOTIFY_FORM_TOKEN_PREFIX", "WhatsAppChannel", "send_form_ask_flow"]
