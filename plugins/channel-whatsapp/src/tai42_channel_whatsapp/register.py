"""Registration side-effects: the ``"whatsapp"`` channel and its routes.

Loaded when the manifest's ``channel_modules`` lists ``tai42_channel_whatsapp``.
Importing THIS module registers the channel on ``tai42_app.channels``, (via the
``inbound`` import) the unauthenticated webhook and Flow data routes, and (via the
``provisioning_route`` import) the admin-only door that registers the Flow encryption
public key; importing the package ``__init__`` alone does NOT (library use).

The WhatsApp number's webhook is configured OUT-OF-BAND (Meta App dashboard):
point the webhook callback URL at ``{public base URL}/api/channels/whatsapp/inbound``
and set the verify token to ``CHANNEL_WHATSAPP_VERIFY_TOKEN``. The plugin
never mutates Meta app configuration.
"""

from tai42_contract.app import tai42_app

# Route registration side-effects; the two imports bind one name, so the unused-import
# finding lands on the last of them.
import tai42_channel_whatsapp.inbound
import tai42_channel_whatsapp.provisioning_route  # noqa: F401
from tai42_channel_whatsapp.channel import WhatsAppChannel

tai42_app.channels.register("whatsapp", WhatsAppChannel())
