"""``TAI_AUDIT_LOG_*`` config for the authenticated-request audit log.

See ``tai42_skeleton.middleware.audit_log`` for the line's contract.

``enable`` defaults ON, matching ``ACCESS_CONTROL_ENABLE``. Off means the
middleware is never REGISTERED — no audit code on the request path, not a
registered no-op — so it is read once at app construction, not per request.
"""

from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class AuditLogSettings(TaiBaseSettings):
    """``TAI_AUDIT_LOG_*`` settings for the authenticated-request audit log."""

    model_config = SettingsConfigDict(env_prefix="TAI_AUDIT_LOG_")

    enable: bool = True


@settings_cache
def audit_log_settings() -> AuditLogSettings:
    """The cached :class:`AuditLogSettings`, read once at app construction."""
    return AuditLogSettings()
