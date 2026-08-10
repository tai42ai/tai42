"""Registration side-effects: the channel and its inbound door.

Importing this module registers :class:`SlackChannel` under ``"slack"`` and
imports :mod:`tai42_channel_slack.inbound` so the Events API route registers too.
The manifest names this package in ``channel_modules``; importing the package
``__init__`` alone does NOT register (library use).

No startup hook: the Events API Request URL is configured once in the Slack app
dashboard, not per process start.
"""

from tai42_contract.app import tai42_app

import tai42_channel_slack.inbound  # noqa: F401  (route-registration side-effect)
from tai42_channel_slack.channel import SlackChannel

tai42_app.channels.register("slack", SlackChannel())
