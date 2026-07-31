"""tai42-identity-oidc: the validate-only OIDC identity provider plugin.

Importing this package registers the ``"identity-oidc"`` identity provider in the
contract's module-level registry (see :mod:`tai42_identity_oidc.provider`).
"""

from __future__ import annotations

from tai42_identity_oidc.provider import OidcIdentityProvider, OidcIdentitySettings

__all__ = [
    "OidcIdentityProvider",
    "OidcIdentitySettings",
]
