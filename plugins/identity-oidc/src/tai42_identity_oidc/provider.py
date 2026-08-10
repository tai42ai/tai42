"""Validate-only OIDC identity provider.

Resolves an issuer-minted JWT to an authenticated identity: a structural gate
claims JWT-shaped tokens (non-JWT credentials fall through the provider chain), the
token is verified against the issuer's JWKS via :mod:`tai42_kit.net.jwt`, and the
verified claims become the identity. The subject is namespaced ``idp:{issuer}:{sub}``
so an issuer subject never collides with another principal in the policy namespace.

It implements the base :class:`IdentityProvider` ABC, not
:class:`ApiKeyIdentityProvider`: it mints no keys and holds no state. A subject with
no operator-provisioned policy is denied (deny-by-default). Registers itself as the
``"identity-oidc"`` provider at import.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.identity import (
    AuthIdentity,
    IdentityProvider,
    IdentityProviderSettings,
)
from tai42_contract.access_control.registry import register_identity_provider
from tai42_kit.net.jwt import JwksCache, JwtVerifyError, fetch_discovery, looks_like_jwt, verify_jwt
from tai42_kit.settings import TaiBaseSettings

logger = logging.getLogger(__name__)

# A kid the issuer's live JWKS will not carry, used by the healthcheck to force a
# real JWKS fetch; a fetch that simply lacks it confirms reachability.
_HEALTHCHECK_PROBE_KID = "__identity_oidc_healthcheck_probe__"


class OidcIdentitySettings(TaiBaseSettings):
    """``TAI_IDENTITY_OIDC_*`` config for the validate-only OIDC provider.

    ``issuer`` and ``audience`` are REQUIRED — ``| None`` at the model level (so
    settings load cleanly when the process does not run this provider) and enforced
    loudly at provider construction. ``allowed_algs`` is an explicit allowlist
    (``alg=none`` and symmetric downgrades rejected by construction).
    """

    model_config = SettingsConfigDict(env_prefix="TAI_IDENTITY_OIDC_")

    issuer: str | None = None
    audience: str | None = None
    # An empty allowlist would reject every token; refused loudly at load.
    allowed_algs: tuple[str, ...] = Field(default=("RS256",), min_length=1)
    claim: str = "sub"
    jwks_ttl_seconds: float = 3600.0


def _require(value: str | None, env_name: str) -> str:
    """Return ``value``, or raise loudly naming the env var when it is unset."""
    if not value:
        raise ValueError(f"{env_name} is required for the OIDC identity provider.")
    return value


class OidcIdentityProvider(IdentityProvider):
    """Validate an issuer-minted JWT against the issuer's JWKS.

    Implements the base :class:`IdentityProvider` ABC, not
    :class:`ApiKeyIdentityProvider`, so it exposes no
    ``provision``/``revoke``/``list_identities`` surface. It reads its own
    ``TAI_IDENTITY_OIDC_*`` env config and uses nothing from the injected settings;
    holding no state, it inherits the empty ``readiness_targets`` (``healthcheck``
    is the issuer-reachability check).
    """

    def __init__(self, settings: IdentityProviderSettings) -> None:
        # The injected AC settings object satisfies the factory contract but is
        # unused; a missing required issuer/audience raises loudly here.
        self._settings = OidcIdentitySettings()
        self._issuer = _require(self._settings.issuer, "TAI_IDENTITY_OIDC_ISSUER")
        self._audience = _require(self._settings.audience, "TAI_IDENTITY_OIDC_AUDIENCE")
        # JWKS uri is known only after discovery, so the cache is built lazily under
        # a lock (concurrent first callers collapse behind one discovery fetch).
        self._jwks: JwksCache | None = None
        self._discovery_lock = asyncio.Lock()

    async def _jwks_cache(self) -> JwksCache:
        if self._jwks is not None:
            return self._jwks
        async with self._discovery_lock:
            if self._jwks is None:
                discovery = await fetch_discovery(self._issuer)
                self._jwks = JwksCache(discovery.jwks_uri, ttl_seconds=self._settings.jwks_ttl_seconds)
            return self._jwks

    async def validate_token(self, token: str) -> AuthIdentity | None:
        # Structural gate: a non-JWT credential is not ours — return None so it falls
        # through the provider chain with no verification attempt and no fetch.
        if not looks_like_jwt(token):
            return None

        # A JWT-shaped token that fails verification raises a typed error, which
        # propagates so the request denies (fail-closed).
        jwks = await self._jwks_cache()
        claims = await verify_jwt(
            token,
            jwks=jwks,
            issuer=self._issuer,
            audience=self._audience,
            allowed_algs=self._settings.allowed_algs,
        )

        # Reserved-claim strip (defense-in-depth): drop any issuer-emitted
        # ``owner_user_id``, which is authoritative only from the key-mint path.
        claims = {name: value for name, value in claims.items() if name != OWNER_USER_ID_CLAIM}

        if self._settings.claim not in claims:
            # A verified token missing the configured subject claim is a
            # misconfiguration — raise so it is visible.
            raise ValueError(
                f"Verified token from issuer {self._issuer!r} has no "
                f"{self._settings.claim!r} claim to map to a user id."
            )

        # Namespace the subject: the ``idp:{issuer}:`` prefix fences issuer subjects
        # into their own namespace so a bare sub cannot inherit another principal's
        # policy. Operators pre-provision policy under this namespaced id.
        user_id = f"idp:{self._issuer}:{claims[self._settings.claim]}"
        return AuthIdentity(user_id=user_id, claims=claims)

    async def healthcheck(self) -> None:
        # Prove the issuer's discovery + JWKS are reachable and well-formed at boot.
        # ``get_key`` forces a real fetch; a missing probe kid is not a failure (the
        # JWKS was fetched and parsed), but a transport/parse/size failure propagates.
        discovery = await fetch_discovery(self._issuer)
        probe_cache = JwksCache(discovery.jwks_uri, ttl_seconds=self._settings.jwks_ttl_seconds)
        probe_alg = self._settings.allowed_algs[0]
        try:
            await probe_cache.get_key(_HEALTHCHECK_PROBE_KID, probe_alg)
        except JwtVerifyError:
            return


# Registers itself as "identity-oidc" at import (no ``tai42_app`` handle). The
# factory is the class itself.
register_identity_provider("identity-oidc", OidcIdentityProvider)
