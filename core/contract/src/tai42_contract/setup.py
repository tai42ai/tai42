"""The setup-door request and result shapes.

``POST /api/setup`` initializes a deployment once: it creates the owner
principal, mints the owner's first key, and — when a login-attaching accounts
provider is configured — attaches the owner's login. These are the wire shapes
the door reads and returns; the door's logic lives in the application layer.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.accounts.models import LoginCredential


class SetupRequest(BaseModel):
    """The body of ``POST /api/setup``.

    ``setup_token`` is checked against the deployment's setup token.
    ``owner_user_id`` and ``key_user_id`` are optional — the door mints ids when
    they are absent. ``owner_display_name`` is required and non-empty (the
    principals door rejects an empty display name). ``login`` attaches the owner's
    interactive login when a login-attaching provider is configured; a keys-only
    deployment sends ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    setup_token: str = ""
    owner_user_id: str | None = None
    owner_display_name: str = Field(min_length=1)
    key_user_id: str | None = None
    key_description: str = "owner key"
    login: LoginCredential | None = None


class SetupResult(BaseModel):
    """The result of a successful ``POST /api/setup``.

    ``api_key`` is the owner key's plaintext, returned exactly once.
    ``login_attached`` is whether an interactive login was attached;
    ``invite_token`` and ``login_path`` are set only when the login was an invite
    the operator completes later.
    """

    model_config = ConfigDict(extra="forbid")

    owner_user_id: str
    key_user_id: str
    api_key: str
    key_fingerprint: str
    login_attached: bool
    invite_token: str | None = None
    login_path: str | None = None


__all__ = ["SetupRequest", "SetupResult"]
