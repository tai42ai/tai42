"""Registration side-effects: the channel and its inbound door.

Importing this module registers :class:`SlackChannel` under ``"slack"`` and
imports :mod:`tai42_channel_slack.inbound` so the Events API route registers too.
The manifest names this package in ``channel_modules``; importing the package
``__init__`` alone does NOT register (library use).

The Events API Request URL is configured once in the Slack app dashboard, not per
process start, so there is no webhook to set at boot. The one startup hook is a
config guard: the inbound door bridges under this deployment's single bot
identity, so ``bot_user_id`` MUST be set — without it the self-message filter is
bypassed and the bridge runs with no identity, failing per inbound event. The
guard fails boot loudly instead.
"""

from tai42_contract.app import tai42_app
from tai42_kit.settings import require

import tai42_channel_slack.inbound  # noqa: F401  (route-registration side-effect)
from tai42_channel_slack.channel import SlackChannel
from tai42_channel_slack.settings import slack_settings

tai42_app.channels.register("slack", SlackChannel())


@tai42_app.lifecycle.on_startup
async def _require_bot_user_id() -> None:
    """Abort startup when the slack channel is loaded without ``bot_user_id``.

    The inbound Events API door registered above bridges under this deployment's
    single bot identity, so a missing ``bot_user_id`` cannot be a per-inbound
    failure — it is a misconfiguration surfaced at boot, naming the env var."""
    require(slack_settings().bot_user_id, "the slack channel", "CHANNEL_SLACK_BOT_USER_ID")
