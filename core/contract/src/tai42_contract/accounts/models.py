"""Data models for the accounts plugin kind.

An accounts provider declares HOW a human can sign in as data; the Studio
login shell renders that data. Exactly two login-method shapes exist: a
credentials form posted to a provider route, and a button linking to a provider
redirect flow. The provider supplies every piece of content (titles, labels,
icons, paths); the renderer supplies only the two shapes.

The same module carries the credential shapes a provider attaches to a principal
and the membership shapes it lists — the people it owns and the invitations it
holds — so a generic Members view can render every provider's accounts without
naming any of them.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ROUTE_PATH_CHARS = re.compile(r"\A[A-Za-z0-9._~/%-]*\Z")
_DOUBLE_DOT_SEGMENTS = frozenset({"..", ".%2e", "%2e.", "%2e%2e"})


def _require_relative_path(value: str) -> str:
    """Reject anything but a same-origin path confined to ``/api/``.

    The login renderer POSTs to ``submit_path`` and navigates to ``href``
    verbatim; absolute (``https://…``) and protocol-relative (``//…``) values
    would steer the pre-auth screen off the deployment origin, and every
    legitimate target is a plain server route under ``/api/``. The value is
    restricted to unreserved URL characters, ``/`` and percent-encoding, so a
    browser resolves it with the exact segmentation checked here — control
    characters, spaces and backslashes, which a browser would strip or fold
    into different segments, are rejected outright. A ``..`` segment (including
    its ``%2e`` spellings) is then rejected so a target cannot climb back out
    of ``/api/``.
    """
    if _ROUTE_PATH_CHARS.match(value) is None:
        raise ValueError(f"must be a plain same-origin route path, got {value!r}")
    if not value.startswith("/api/"):
        raise ValueError(f"must be a same-origin path starting with '/api/', got {value!r}")
    if any(segment.lower() in _DOUBLE_DOT_SEGMENTS for segment in value.split("/")):
        raise ValueError(f"must not contain a '..' path segment, got {value!r}")
    return value


class FormField(BaseModel):
    """One input of a form-shaped login method."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="JSON key sent to submit_path.")
    label: str = Field(min_length=1, description="Human-readable label the renderer displays.")
    secret: bool = Field(default=False, description="Render as a password input; value is never echoed.")
    autocomplete: str | None = Field(
        default=None,
        description='Browser autocomplete hint, e.g. "email" or "current-password".',
    )


class FormMethod(BaseModel):
    """A credentials form: the renderer draws the fields and POSTs them as JSON to submit_path."""

    model_config = ConfigDict(extra="forbid")

    shape: Literal["form"] = "form"
    id: str = Field(min_length=1, description="Stable method id, unique within the provider.")
    title: str = Field(min_length=1)
    purpose: Literal["login", "invite"] = Field(
        default="login",
        description=(
            'What the form is FOR: "login" renders always; "invite" is the set-your-password '
            "form (shown when the login URL carries an invite token)."
        ),
    )
    fields: list[FormField] = Field(min_length=1)
    submit_path: str

    @field_validator("submit_path")
    @classmethod
    def _submit_path_relative(cls, value: str) -> str:
        return _require_relative_path(value)


class ButtonMethod(BaseModel):
    """A redirect button: the renderer draws a button that navigates to href (e.g. an OIDC authorize route)."""

    model_config = ConfigDict(extra="forbid")

    shape: Literal["button"] = "button"
    id: str = Field(min_length=1, description="Stable method id, unique within the provider.")
    label: str = Field(min_length=1)
    icon: str | None = Field(default=None, description="Inline SVG markup, or None for a text-only button.")
    href: str

    @field_validator("href")
    @classmethod
    def _href_relative(cls, value: str) -> str:
        return _require_relative_path(value)


LoginMethod = Annotated[FormMethod | ButtonMethod, Field(discriminator="shape")]
"""The closed union of renderable login-method shapes."""


class PasswordCredential(BaseModel):
    """A login credential that sets the principal's password now."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["password"] = "password"
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


class InviteCredential(BaseModel):
    """A login credential that mints a one-time invite link instead of setting a password now."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["invite"] = "invite"
    email: str = Field(min_length=1)


LoginCredential = Annotated[PasswordCredential | InviteCredential, Field(discriminator="kind")]
"""The closed union of login credentials a provider can attach to a principal."""


class LoginAttachment(BaseModel):
    """The outcome of attaching a login to a principal.

    ``attached`` is whether an interactive login was attached. ``invite_token``
    and ``login_path`` are set only for an invite credential — the one-time link
    the operator follows to set the password later.
    """

    model_config = ConfigDict(extra="forbid")

    attached: bool
    invite_token: str | None = None
    login_path: str | None = None


class MemberEntry(BaseModel):
    """One person an accounts provider owns: an account the operator manages.

    ``id`` is the provider's stable handle for the person — the id its own member
    routes act on. ``email`` is the sign-in identity the provider holds for them.
    ``role`` names the platform role the person holds; only the name rides here, the
    role's definition is read from the role listing. ``disabled`` turns off the
    account; ``created_at`` is timezone-aware (UTC).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    email: str = Field(min_length=1)
    role: str = Field(min_length=1)
    disabled: bool
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _created_at_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)


class InviteEntry(BaseModel):
    """One outstanding invitation an accounts provider holds.

    ``id`` is the provider's stable handle for the invited person — the id its own
    invite routes act on. ``email`` is the invited address; ``role`` names the platform
    role the person will hold (only the name, as on :class:`MemberEntry`). ``created_at``
    and ``expires_at`` are timezone-aware (UTC).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    email: str = Field(min_length=1)
    role: str = Field(min_length=1)
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def _timestamp_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return value.astimezone(UTC)


class MemberListing(BaseModel):
    """An accounts provider's membership: the people it owns and the invitations it holds.

    Returned by :meth:`~tai42_contract.accounts.provider.AccountsProvider.list_members`.
    The application's Members aggregation concatenates one of these per registered
    accounts provider into a single deployment-wide view, so a provider's people and
    outstanding invitations appear without the platform naming the provider.
    """

    model_config = ConfigDict(extra="forbid")

    members: list[MemberEntry]
    invites: list[InviteEntry]


__all__ = [
    "ButtonMethod",
    "FormField",
    "FormMethod",
    "InviteCredential",
    "InviteEntry",
    "LoginAttachment",
    "LoginCredential",
    "LoginMethod",
    "MemberEntry",
    "MemberListing",
    "PasswordCredential",
]
