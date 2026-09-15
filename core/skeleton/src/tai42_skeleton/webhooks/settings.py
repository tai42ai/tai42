"""``WEBHOOK_INGRESS_*`` config for the public ``universal_webhook`` door.

The public webhook ingress reads an untrusted body and query string into memory
and parses hostile content types, so both are bounded by a settings cap.
"""

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class WebhookIngressSettings(TaiBaseSettings):
    """``WEBHOOK_INGRESS_*`` config bounding the public ``universal_webhook`` door's read size."""

    model_config = SettingsConfigDict(env_prefix="WEBHOOK_INGRESS_", frozen=True)

    # Hard cap (bytes) on the request body AND the raw query string the public
    # ``universal_webhook`` door reads into memory. Payload can ride either, so
    # both obey this cap. Oversized -> 413, loudly — never truncated. Must be
    # positive.
    max_body_bytes: int = Field(default=65536, gt=0)


@settings_cache
def webhook_ingress_settings() -> WebhookIngressSettings:
    """Return the process-wide :class:`WebhookIngressSettings`, cached after first load."""
    return WebhookIngressSettings()
