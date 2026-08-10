"""Pydantic-settings for the marketplace client.

:class:`MarketplaceSettings` (``MARKETPLACE_*``) carries the registry endpoint plus
the advisory-poll knobs. The ``marketplace_installs`` attribution table lives in
the skeleton component's bound database, resolved through the central registry.

The advisory poll is the ONLY background outbound call this feature makes, and
it is a visible, documented setting: ``MARKETPLACE_ADVISORIES_POLL`` defaults to
on, the startup log names the polled URL, and one env var turns it off. Nothing
else is ever sent to the registry without an explicit operator request.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class MarketplaceSettings(TaiBaseSettings):
    """``MARKETPLACE_*`` — the registry endpoint and the advisory-poll knobs."""

    model_config = SettingsConfigDict(env_prefix="MARKETPLACE_")

    # Base URL of the marketplace registry's public API.
    url: str = "https://marketplace.tai42.ai"

    # Periodically re-fetch advisories for the installed plugins. A background
    # outbound call, so it is explicit and documented: default on, loud startup
    # log naming the URL, one env var to disable. Install-time advisory checks
    # are separate and unconditional.
    advisories_poll: bool = True

    # Seconds between advisory polls (and the freshness bound the advisories
    # route serves within). Must be positive.
    advisories_interval_s: int = Field(default=3600, gt=0)

    # Per-request timeout (seconds) for registry calls. Must be positive.
    request_timeout_s: float = Field(default=15, gt=0)


@settings_cache
def marketplace_settings() -> MarketplaceSettings:
    return MarketplaceSettings()
