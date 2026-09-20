"""Meta WhatsApp API channel plugin.

A ``tai42_contract.channels.Channel`` that delivers ``ask_user`` questions over
the WhatsApp (Meta Graph) API and bridges replies back through its own
webhook route. Importing this package does NOT register anything (library use);
the runtime imports ``tai42_channel_whatsapp.register`` to register the
``"whatsapp"`` channel and its inbound route.
"""

from tai42_channel_whatsapp.channel import WhatsAppChannel
from tai42_channel_whatsapp.settings import WhatsAppSettings, whatsapp_settings

__all__ = ["WhatsAppChannel", "WhatsAppSettings", "whatsapp_settings"]
