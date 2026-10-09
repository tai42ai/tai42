"""A fixture identity provider that declares one OAuth authorization server — the MCP door's e2e consumer.

It proves the declaration seam end to end: it declares the authorization server named by
``E2E_DOOR_AUTHORIZATION_SERVER`` and accepts exactly one static token
(``E2E_DOOR_STATIC_TOKEN``) as the principal ``E2E_DOOR_USER_ID`` — deliberately not a token
validator of any standard kind. A manifest activates it by naming this module in
``lifecycle_modules`` and adding ``door-static`` to ``ACCESS_CONTROL_AUTH_PROVIDERS``; the
module-level registration fires at import.
"""

from __future__ import annotations

import os

from tai42_contract.access_control.identity import AuthIdentity, IdentityProvider, IdentityProviderSettings
from tai42_kit.access_control.registry import register_identity_provider

PROVIDER_NAME = "door-static"
AUTHORIZATION_SERVER_ENV = "E2E_DOOR_AUTHORIZATION_SERVER"
STATIC_TOKEN_ENV = "E2E_DOOR_STATIC_TOKEN"
USER_ID_ENV = "E2E_DOOR_USER_ID"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"the {PROVIDER_NAME!r} identity provider needs {name} set")
    return value


class DoorStaticTokenProvider(IdentityProvider):
    """Declares one authorization server and accepts one static token."""

    def __init__(self, settings: IdentityProviderSettings) -> None:
        # The access-control settings carry nothing this provider reads; its values are its own env.
        del settings
        self._server = _required(AUTHORIZATION_SERVER_ENV)
        self._token = _required(STATIC_TOKEN_ENV)
        self._user_id = _required(USER_ID_ENV)

    async def validate_token(self, token: str) -> AuthIdentity | None:
        if token != self._token:
            return None
        return AuthIdentity(user_id=self._user_id, claims={})

    def authorization_servers(self) -> tuple[str, ...]:
        return (self._server,)


register_identity_provider(PROVIDER_NAME, DoorStaticTokenProvider)
