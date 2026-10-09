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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tai42_contract.template import TemplatedText

# A member's platform role name, or ``None`` when the principal's policy was not written
# from a role template.
_RoleName = Annotated[str, Field(min_length=1)] | None

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


MemberActionScope = Literal["page", "member_row", "invite_row"]
"""Where a declared member action renders: a page-level button, a member-row menu item,
or an invite-row menu item. A provider classifies each of its own actions into one of
these three places; the platform never reads the provider's meaning behind it."""


class MemberAction(BaseModel):
    """One member-admin action an accounts provider declares.

    An IN-PROCESS declaration returned by
    :meth:`~tai42_contract.accounts.provider.AccountsProvider.member_actions`. The
    platform serializes a wire :class:`MemberActionDescriptor` from it and validates an
    invoke's input by calling ``input_model.model_validate(input)`` (plain pydantic).
    ``input_model`` and ``result_model`` are OPAQUE to the platform: it reads no field of
    either, it only renders their JSON schema and runs the ordinary model validation and
    dump. ``id`` is the provider's OWN action id — the platform maps it to an opaque wire
    key and never parses or branches on it. ``label`` is a :class:`TemplatedText`
    reference (a stored template id or inline content), so the wording is localizable
    through the platform's existing resource manager and never held as a literal here.

    Not serialized to the wire (it carries class references), so
    ``arbitrary_types_allowed`` is safe.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, frozen=True)

    id: str = Field(min_length=1)
    label: TemplatedText
    scope: MemberActionScope
    destructive: bool = False
    input_model: type[BaseModel]
    result_model: type[BaseModel]


class MemberActionDescriptor(BaseModel):
    """One declared member action, serialized for the catalog wire.

    ``key`` is the opaque catalog key the platform mints for (provider, action) — a caller
    joins and echoes it, never parses it. ``label`` is the resolved wording (the
    declaration's :class:`TemplatedText` rendered at the door). ``input_schema`` and
    ``result_schema`` are the declared models' ``model_json_schema()`` — a caller renders
    a form and a read-only result view from them generically, with no provider field name
    known to the platform.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    label: str
    scope: MemberActionScope
    destructive: bool
    input_schema: dict[str, Any]
    result_schema: dict[str, Any]


class MemberActionCatalog(BaseModel):
    """Every declared member action across the registered accounts providers."""

    model_config = ConfigDict(extra="forbid")

    actions: list[MemberActionDescriptor]


class MemberEntry(BaseModel):
    """One person an accounts provider owns: an account the operator manages.

    ``id`` is the provider's stable handle for the person — the id its own actions act on.
    ``email`` is the sign-in identity the provider holds for them. ``role`` names the
    platform role the person holds; only the name rides here, the role's definition is read
    from the role listing. ``None`` — the principal's policy was not written from a role
    template. ``created_at`` is timezone-aware (UTC).

    ``principal_ids`` are the platform principal id(s) this person holds (at least one — a
    person with none is an invitation, not a member); the Members operation joins them
    against the platform's own principal records to resolve the access-control
    ``disabled`` state, which is NOT a provider fact and so does not ride here.
    ``actions`` are the provider's OWN member-action ids applicable to this row.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    email: str = Field(min_length=1)
    role: _RoleName
    created_at: datetime
    principal_ids: list[str] = Field(min_length=1)
    actions: list[str] = Field(default_factory=list)

    @field_validator("created_at")
    @classmethod
    def _created_at_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)


class InviteEntry(BaseModel):
    """One outstanding invitation an accounts provider holds.

    ``id`` is the provider's stable handle for the invited person — the id its own actions
    act on. ``email`` is the invited address; ``role`` names the platform role the person
    will hold (only the name, as on :class:`MemberEntry`; ``None`` — the principal's policy
    was not written from a role template). ``created_at`` and
    ``expires_at`` are timezone-aware (UTC). ``actions`` are the provider's OWN
    member-action ids applicable to this row. An invitation holds no principal yet, so it
    carries no ``principal_ids``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    email: str = Field(min_length=1)
    role: _RoleName
    created_at: datetime
    expires_at: datetime
    actions: list[str] = Field(default_factory=list)

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


class MemberPrincipalState(BaseModel):
    """One platform principal a member holds, with its access-control state joined in.

    ``user_id`` is the platform principal id; ``disabled`` is taken from that principal's
    own record (the store the principals listing reads), never from a provider.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    disabled: bool


class MemberRow(BaseModel):
    """A member row in the aggregated directory the Members door returns.

    Display fields plus the platform-joined principal state and opaque routing tokens.
    ``handle`` routes an invoke back to the producing provider and pins this row;
    ``action_keys`` are the opaque catalog keys of the actions applicable to it — a caller
    joins and echoes both, never parses them. ``principals`` carries each principal's
    joined state; ``disabled`` is derived True only when EVERY principal is disabled, so a
    person with any enabled principal still reads active while the per-principal truth
    stays visible. ``role`` is provider-sourced; ``None`` — the principal's policy was not
    written from a role template.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    email: str = Field(min_length=1)
    role: _RoleName
    created_at: datetime
    principals: list[MemberPrincipalState]
    disabled: bool
    handle: str = Field(min_length=1)
    action_keys: list[str] = Field(default_factory=list)


class InviteRow(BaseModel):
    """An invite row in the aggregated directory the Members door returns.

    Display fields plus the opaque routing tokens. An invitation holds no principal, so it
    carries no principal state; ``role`` is provider-sourced; ``None`` — the principal's
    policy was not written from a role template.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    email: str = Field(min_length=1)
    role: _RoleName
    created_at: datetime
    expires_at: datetime
    handle: str = Field(min_length=1)
    action_keys: list[str] = Field(default_factory=list)


class MemberDirectory(BaseModel):
    """The aggregated, platform-joined membership the Members door returns.

    The wire shape of the deployment-wide Members view: every provider's people and
    invitations as :class:`MemberRow` / :class:`InviteRow`, each carrying opaque routing
    tokens and (for a member) the platform-joined principal state. The provider's raw
    action ids and principal ids never reach this wire.
    """

    model_config = ConfigDict(extra="forbid")

    members: list[MemberRow]
    invites: list[InviteRow]


class InvokeMemberActionRequest(BaseModel):
    """The invoke-door envelope: which action, which row, and the per-action input.

    ``action_key`` is the opaque catalog key; ``target_handle`` is the row handle an
    action acts on (``None`` for a page-scoped action); ``input`` is an open mapping the
    operation hands to the declared ``input_model`` for ordinary validation, so the
    platform's static envelope never needs to know the per-action fields. Unknown
    top-level keys are ignored (pydantic's default extra behaviour).
    """

    action_key: str = Field(min_length=1)
    target_handle: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class InvokeMemberActionResult(BaseModel):
    """The invoke-door result: the provider's result-model dump, carried opaquely.

    ``result`` is the ``model_dump(mode="json")`` of the action's declared
    ``result_model`` instance; the platform reads no field of it.
    """

    model_config = ConfigDict(extra="forbid")

    result: dict[str, Any]


__all__ = [
    "ButtonMethod",
    "FormField",
    "FormMethod",
    "InviteCredential",
    "InviteEntry",
    "InviteRow",
    "InvokeMemberActionRequest",
    "InvokeMemberActionResult",
    "LoginAttachment",
    "LoginCredential",
    "LoginMethod",
    "MemberAction",
    "MemberActionCatalog",
    "MemberActionDescriptor",
    "MemberActionScope",
    "MemberDirectory",
    "MemberEntry",
    "MemberListing",
    "MemberPrincipalState",
    "MemberRow",
    "PasswordCredential",
]
