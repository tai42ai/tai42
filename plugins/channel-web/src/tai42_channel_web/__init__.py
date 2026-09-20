"""Public web chat channel plugin.

A ``tai42_contract.channels.Channel`` that delivers ``ask_user`` questions into a
visitor's chat page and bridges their reply back through its own public
``/api/channels/web`` doors. Importing this package does NOT register anything
(library use); the runtime imports ``tai42_channel_web.register`` to register the
``"web"`` channel and its routes.
"""

from tai42_channel_web.channel import WebChannel
from tai42_channel_web.settings import WebSettings, web_settings

__all__ = ["WebChannel", "WebSettings", "web_settings"]
