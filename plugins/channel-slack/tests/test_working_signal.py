"""Slack opts out of the working-on-it signal: the Web API has no bot typing
indicator, so the channel declares ``working_signal_expiry_seconds = None`` and
implements no ``signal_working`` — the skeleton loop never starts for it."""

from __future__ import annotations

from tai42_channel_slack import SlackChannel


def test_slack_opts_out_of_working_signal():
    assert SlackChannel.working_signal_expiry_seconds is None
    assert not hasattr(SlackChannel, "signal_working")
