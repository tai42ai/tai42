"""The process-wide channel registry — the body behind ``app.channels``.

A channel plugin registers under a name via an import-only ``channel_modules``
manifest entry (importing the module runs its ``tai42_app.channels.register(...)``
call). The ``ask`` helper resolves a named channel at ask time.

The registry is reset on every ``start()`` (like the webhook-verifier registry)
so a reload re-imports the channel modules and re-registers cleanly; a duplicate
name within one load raises loudly (a silent overwrite could swap the deliverer
out from under a live question).
"""

from __future__ import annotations

from tai42_contract.app import tai42_app
from tai42_contract.channels import Channel


class ChannelRegistry:
    """The process-wide name-to-:class:`Channel` map behind ``app.channels``."""

    def __init__(self) -> None:
        """Start with an empty channel map."""
        self._channels: dict[str, Channel] = {}

    def register(self, name: str, channel: Channel) -> None:
        """Register ``channel`` under ``name``; raise if the name is already taken."""
        if name in self._channels:
            raise ValueError(f"channel {name!r} is already registered")
        self._channels[name] = channel

    def get(self, name: str) -> Channel:
        """Return the channel registered under ``name``; raise ``KeyError`` if unknown."""
        try:
            return self._channels[name]
        except KeyError:
            raise KeyError(f"unknown channel {name!r} (registered: {sorted(self._channels)})") from None

    def names(self) -> list[str]:
        """The registered channel names, sorted."""
        return sorted(self._channels)

    def reset(self) -> None:
        """Clear every registration, so a reload can re-register cleanly."""
        self._channels.clear()


def resolve_channel(channel: str) -> Channel:
    """Resolve a named channel on the app handle loudly — a malformed or unknown name raises ``ValueError``.

    A hard guard, never a soft ignore: every caller that hands work to a named channel resolves it here.
    """
    if not isinstance(channel, str) or not channel:
        raise ValueError("channel must be a non-empty string")
    try:
        return tai42_app.channels.get(channel)
    except KeyError as exc:
        raise ValueError(f"unknown channel: {channel!r}") from exc
