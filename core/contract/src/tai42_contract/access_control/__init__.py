"""Access-control contract: identity, policy/context models, and the ``Verifier`` protocol."""

from __future__ import annotations

from tai42_contract.access_control.context import (
    caller_may_read_secrets,
    get_current_user_id,
    reset_request_secret_capability,
    reset_request_user_id,
    set_request_secret_capability,
    set_request_user_id,
)
from tai42_contract.access_control.identity import (
    KEY_FINGERPRINT_CLAIM,
    OWNER_USER_ID_CLAIM,
    ApiKeyIdentityProvider,
    AuthIdentity,
    IdentityProvider,
    IdentityProviderSettings,
)
from tai42_contract.access_control.models import (
    UNIVERSAL_SCOPE,
    AccessPolicy,
    IdentityRecord,
    JqAuthContext,
    Principal,
    RoleDefinition,
)
from tai42_contract.access_control.verifier import Verifier

__all__ = [
    "KEY_FINGERPRINT_CLAIM",
    "OWNER_USER_ID_CLAIM",
    "UNIVERSAL_SCOPE",
    "AccessPolicy",
    "ApiKeyIdentityProvider",
    "AuthIdentity",
    "IdentityProvider",
    "IdentityProviderSettings",
    "IdentityRecord",
    "JqAuthContext",
    "Principal",
    "RoleDefinition",
    "Verifier",
    "caller_may_read_secrets",
    "get_current_user_id",
    "reset_request_secret_capability",
    "reset_request_user_id",
    "set_request_secret_capability",
    "set_request_user_id",
]
