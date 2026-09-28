"""Web opts out of the server-driven working-on-it signal: the chat page runs its
own client-side typing bubble, so the channel declares
``working_signal_expiry_seconds = None`` and implements no ``signal_working`` —
the skeleton loop never starts for it."""

from __future__ import annotations

from tai42_channel_web import WebChannel


def test_web_opts_out_of_working_signal():
    assert WebChannel.working_signal_expiry_seconds is None
    assert not hasattr(WebChannel, "signal_working")
