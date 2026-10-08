"""The accounts plugin kind: user accounts, login flows, sessions.

Import surface for both sides of the contract: provider implementations
subclass ``AccountsProvider``; the application renders the declared
``LoginMethod`` metadata.
"""

from __future__ import annotations

from tai42_contract.accounts.errors import (
    LoginAttachError,
    LoginConflictError,
    MemberActionBadRequestError,
    MemberActionConflictError,
    MemberActionError,
    MemberActionNotFoundError,
)
from tai42_contract.accounts.models import (
    ButtonMethod,
    FormField,
    FormMethod,
    InviteCredential,
    InviteEntry,
    InviteRow,
    InvokeMemberActionRequest,
    InvokeMemberActionResult,
    LoginAttachment,
    LoginCredential,
    LoginMethod,
    MemberAction,
    MemberActionCatalog,
    MemberActionDescriptor,
    MemberActionScope,
    MemberDirectory,
    MemberEntry,
    MemberListing,
    MemberPrincipalState,
    MemberRow,
    PasswordCredential,
)
from tai42_contract.accounts.provider import (
    AccountsAdminServices,
    AccountsProvider,
    AccountsProviderSettings,
    LoginAttachingProvider,
)

__all__ = [
    "AccountsAdminServices",
    "AccountsProvider",
    "AccountsProviderSettings",
    "ButtonMethod",
    "FormField",
    "FormMethod",
    "InviteCredential",
    "InviteEntry",
    "InviteRow",
    "InvokeMemberActionRequest",
    "InvokeMemberActionResult",
    "LoginAttachError",
    "LoginAttachingProvider",
    "LoginAttachment",
    "LoginConflictError",
    "LoginCredential",
    "LoginMethod",
    "MemberAction",
    "MemberActionBadRequestError",
    "MemberActionCatalog",
    "MemberActionConflictError",
    "MemberActionDescriptor",
    "MemberActionError",
    "MemberActionNotFoundError",
    "MemberActionScope",
    "MemberDirectory",
    "MemberEntry",
    "MemberListing",
    "MemberPrincipalState",
    "MemberRow",
    "PasswordCredential",
]
