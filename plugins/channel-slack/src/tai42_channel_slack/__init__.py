"""Slack channel plugin.

Delivers ``ask_user`` questions to a configured Slack channel and bridges
threaded replies back through the interactions callback door. Importing this
package does NOT register anything (library use); the runtime imports
:mod:`tai42_channel_slack.register` to register the ``"slack"`` channel and its
inbound route.
"""

from tai42_channel_slack.channel import SlackChannel
from tai42_channel_slack.settings import SlackSettings, slack_settings

__all__ = ["SlackChannel", "SlackSettings", "slack_settings"]
