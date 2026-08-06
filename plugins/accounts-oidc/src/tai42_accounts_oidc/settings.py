"""Plugin configuration — the ``TAI_ACCOUNTS_OIDC_*`` env namespace.

``providers`` is parsed from ``TAI_ACCOUNTS_OIDC_PROVIDERS`` as JSON. ``state_key``
and ``public_base_url`` are REQUIRED, but carry a ``None`` sentinel default so a
missing value surfaces as a precise ``ValueError`` naming the env var at provider
construction rather than an opaque settings-load failure.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache

# The presets a provider row may name. ``github`` selects the plain-OAuth2 path;
# the rest are OIDC discovery issuers.
PRESET_NAMES = frozenset({"auth0", "google", "okta", "keycloak", "azure", "github"})

_SLUG = re.compile(r"^[a-z0-9-]+$")

# The default OIDC scope set (a tuple so no config shares a mutable default).
_DEFAULT_SCOPES = ("openid", "email", "profile")

# The authorize-request parameters the login flow itself owns and builds. An
# operator's extra_authorize_params may not name any of these — a collision would
# silently steer the flow, so it is a config error raised at parse.
_RESERVED_AUTHORIZE_PARAMS = frozenset(
    {"response_type", "client_id", "redirect_uri", "scope", "state", "code_challenge", "code_challenge_method", "nonce"}
)


class ProviderDisplay(BaseModel):
    """Button presentation for one provider.

    ``label`` may be left empty — a preset fills it. ``icon`` is an optional
    operator-supplied inline SVG string (``currentColor`` convention).
    """

    model_config = ConfigDict(extra="forbid")

    label: str = ""
    icon: str | None = None


class OidcProviderConfig(BaseModel):
    """One configured login provider (a row of ``TAI_ACCOUNTS_OIDC_PROVIDERS``)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="URL-safe slug; unique across the providers list.")
    preset: str | None = Field(default=None, description="One of PRESET_NAMES, or None for a raw OIDC issuer.")
    issuer: str | None = Field(default=None, description="OIDC issuer URL; required unless a preset fixes it.")
    client_id: str
    client_secret: SecretStr
    scopes: list[str] = Field(default_factory=lambda: list(_DEFAULT_SCOPES))
    claim: str = Field(default="sub", description="The id_token/userinfo claim mapped into the user id.")
    display: ProviderDisplay = Field(default_factory=ProviderDisplay)
    extra_authorize_params: dict[str, str] = Field(
        default_factory=dict,
        description="Static params merged into the authorize redirect; keys may not collide with the reserved set.",
    )

    @field_validator("name")
    @classmethod
    def _slug_name(cls, value: str) -> str:
        if _SLUG.match(value) is None:
            raise ValueError(f"provider name {value!r} must match {_SLUG.pattern}")
        return value

    @field_validator("preset")
    @classmethod
    def _known_preset(cls, value: str | None) -> str | None:
        if value is not None and value not in PRESET_NAMES:
            raise ValueError(f"unknown preset {value!r}; expected one of {sorted(PRESET_NAMES)}")
        return value

    @field_validator("extra_authorize_params")
    @classmethod
    def _no_reserved_authorize_params(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if key in _RESERVED_AUTHORIZE_PARAMS:
                raise ValueError(
                    f"extra_authorize_params key {key!r} collides with the flow-owned authorize parameter set "
                    f"{sorted(_RESERVED_AUTHORIZE_PARAMS)}"
                )
        return value


def _normalize_public_base_url(value: str) -> str:
    """Validate an absolute ``https`` base URL (loopback ``http`` excepted) and
    strip a trailing slash so the derived paths never double a separator."""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL must be an absolute URL, got {value!r}")
    loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ValueError(
            f"TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL must be https (plain http only for loopback), got {value!r}"
        )
    return value.rstrip("/")


class OidcAccountsSettings(TaiBaseSettings):
    """``TAI_ACCOUNTS_OIDC_*`` behavior config for the OIDC accounts plugin."""

    model_config = SettingsConfigDict(env_prefix="TAI_ACCOUNTS_OIDC_")

    providers: list[OidcProviderConfig] = Field(default_factory=list)

    # HMAC key for the tamper-evident ``state`` parameter; REQUIRED (None sentinel →
    # precise ValueError at provider construction).
    state_key: SecretStr | None = None

    # The public origin both the OAuth redirect_uri and the /login?sso= hand-back
    # derive from — never the inbound Host/origin. REQUIRED (None sentinel).
    public_base_url: str | None = None

    # Idle expiry is the sliding window; absolute expiry is the hard cap from mint.
    session_idle_seconds: int = 86400
    session_absolute_seconds: int = 2592000

    @field_validator("public_base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_public_base_url(value)

    @model_validator(mode="after")
    def _validate_providers(self) -> OidcAccountsSettings:
        # Reject duplicate names (ambiguous lookup); resolve each row through presets
        # so a preset missing its required issuer raises here, naming the field.
        from tai42_accounts_oidc.presets import resolve_provider

        seen: set[str] = set()
        for config in self.providers:
            if config.name in seen:
                raise ValueError(f"duplicate provider name {config.name!r} in TAI_ACCOUNTS_OIDC_PROVIDERS")
            seen.add(config.name)
            resolve_provider(config)
        return self


@settings_cache
def accounts_oidc_settings() -> OidcAccountsSettings:
    """Return the process-wide :class:`OidcAccountsSettings`, cached after first load."""
    return OidcAccountsSettings()
