"""Channel delivery contracts.

A :class:`Channel` pushes an ``ask`` question to a human on a specific
medium (Telegram, Slack, SMS, ...) and bridges the human's reply back into the
interactions store by forwarding it to the delivery's public ``callback_url``.
Channels are registered on the app handle (``tai42_app.channels``) by channel
plugins and looked up by name when ``ask`` is called with ``channel=...``.
Delivery either returns ``None`` (success) or raises
:class:`ChannelDeliveryError` (any failure) — never a bool.

A channel also sends fire-and-forget notifications: ``notify`` pushes one
:class:`ChannelNotification` to a human with no interaction, no ticket, and no
reply path, under the same loud-failure rule; it returns the per-message ids the
medium assigned the send (empty when the medium exposes none), never a bool.
"""

from __future__ import annotations

from tai42_contract.channels.composition import check_interactive_composition
from tai42_contract.channels.correlation import Correlation, CorrelationStore
from tai42_contract.channels.delivery import ChannelDelivery
from tai42_contract.channels.errors import AnswerForwardError, ChannelDeliveryError, ChannelInputError
from tai42_contract.channels.inbound import InboundAnswerOutcome, InboundAnswerResult, InboundBridge

# Message/address caps and per-option caps are part of the channels attribute surface
# (read by the conversations part shapes and by the ask/notify helpers) though not in the
# ``*`` export; the explicit-alias re-export keeps them importable from this package.
from tai42_contract.channels.notification import NOTIFICATION_ADDRESS_MAX_CHARS as NOTIFICATION_ADDRESS_MAX_CHARS
from tai42_contract.channels.notification import NOTIFICATION_MESSAGE_MAX_CHARS as NOTIFICATION_MESSAGE_MAX_CHARS
from tai42_contract.channels.notification import ChannelNotification
from tai42_contract.channels.options import (
    NOTIFICATION_FOOTER_MAX_CHARS,
    NOTIFICATION_SECTIONS_MAX,
    OPTION_ID_MAX_CHARS,
    LinkOption,
    Option,
    OptionSection,
    ReplyOption,
    check_footer,
    check_header,
    check_options,
    check_sections,
)
from tai42_contract.channels.options import NOTIFICATION_OPTION_MAX_CHARS as NOTIFICATION_OPTION_MAX_CHARS
from tai42_contract.channels.options import NOTIFICATION_OPTIONS_MAX as NOTIFICATION_OPTIONS_MAX
from tai42_contract.channels.protocol import Channel, notify_in_order
from tai42_contract.channels.templates import (
    TEMPLATE_BUTTONS_MAX,
    TEMPLATE_PARAM_MAX_CHARS,
    ChannelTemplate,
    QuickReplyButtonParam,
    TemplateButtonParam,
    UrlButtonParam,
)

__all__ = [
    "NOTIFICATION_FOOTER_MAX_CHARS",
    "NOTIFICATION_SECTIONS_MAX",
    "OPTION_ID_MAX_CHARS",
    "TEMPLATE_BUTTONS_MAX",
    "TEMPLATE_PARAM_MAX_CHARS",
    "AnswerForwardError",
    "Channel",
    "ChannelDelivery",
    "ChannelDeliveryError",
    "ChannelInputError",
    "ChannelNotification",
    "ChannelTemplate",
    "Correlation",
    "CorrelationStore",
    "InboundAnswerOutcome",
    "InboundAnswerResult",
    "InboundBridge",
    "LinkOption",
    "Option",
    "OptionSection",
    "QuickReplyButtonParam",
    "ReplyOption",
    "TemplateButtonParam",
    "UrlButtonParam",
    "check_footer",
    "check_header",
    "check_interactive_composition",
    "check_options",
    "check_sections",
    "notify_in_order",
]
