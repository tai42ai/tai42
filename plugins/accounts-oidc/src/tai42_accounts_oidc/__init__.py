"""tai42-accounts-oidc: the OIDC/OAuth2 login accounts provider plugin.

Importing this package registers the ``"accounts-oidc"`` provider (see
:mod:`tai42_accounts_oidc.provider`). The ``/api/login/*`` routes load separately
through the manifest's ``routers_modules``.
"""

from __future__ import annotations

from tai42_accounts_oidc.provider import OidcAccountsProvider

__all__ = [
    "OidcAccountsProvider",
]
