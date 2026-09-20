"""Registration side-effects: the ``"web"`` channel and its public chat doors.

Loaded when the manifest's ``channel_modules`` lists ``tai42_channel_web``.
Importing THIS module registers the channel on ``tai42_app.channels`` and (via the
``routes`` import) the public ``/api/channels/web`` doors — the chat page, its
assets, and the message/stream/answer/rotate doors; importing the package
``__init__`` alone does NOT (library use).
"""

from tai42_contract.app import tai42_app

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.channel import WebChannel

tai42_app.channels.register("web", WebChannel())
