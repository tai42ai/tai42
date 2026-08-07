"""The accounts plugin kind: user accounts, login flows, sessions.

Import surface for both sides of the contract: provider implementations
subclass ``AccountsProvider`` and call ``register_accounts_provider`` at
module import; the application enumerates ``iter_accounts_provider_factories``
and renders the declared ``LoginMethod`` metadata.
"""

from __future__ import annotations

from tai42_contract.accounts.models import ButtonMethod, FormField, FormMethod, LoginMethod
from tai42_contract.accounts.provider import (
    AccountsAdminServices,
    AccountsProvider,
    AccountsProviderSettings,
)
from tai42_contract.accounts.registry import (
    abort_staging,
    begin_staging,
    commit_staging,
    get_accounts_provider_factory,
    iter_accounts_provider_factories,
    iter_accounts_provider_factories_staged,
    register_accounts_provider,
    reset_registry,
)

__all__ = [
    "AccountsAdminServices",
    "AccountsProvider",
    "AccountsProviderSettings",
    "ButtonMethod",
    "FormField",
    "FormMethod",
    "LoginMethod",
    "abort_staging",
    "begin_staging",
    "commit_staging",
    "get_accounts_provider_factory",
    "iter_accounts_provider_factories",
    "iter_accounts_provider_factories_staged",
    "register_accounts_provider",
    "reset_registry",
]
