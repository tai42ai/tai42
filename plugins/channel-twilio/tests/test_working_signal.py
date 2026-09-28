"""Twilio opts out of the working-on-it signal: SMS/WhatsApp-over-Twilio carries
no typing indicator, so the channel declares ``working_signal_expiry_seconds =
None`` and implements no ``signal_working`` — the skeleton loop never starts for
it."""

from __future__ import annotations

from tai42_channel_twilio import TwilioChannel


def test_twilio_opts_out_of_working_signal():
    assert TwilioChannel.working_signal_expiry_seconds is None
    assert not hasattr(TwilioChannel, "signal_working")
