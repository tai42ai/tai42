"""Fixture registering an accounts provider ON IMPORT — the real accounts-plugin
shape (a module-level ``register_accounts_provider`` call, which dual-writes the
identity registry).

Reloaded via ``import_or_reload_package`` to prove that re-executing a plugin's
module body under the hot-reload primitive is reload-safe: the re-registration of
the same declared provider is a no-op, not an "already registered" crash.
"""

from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.accounts import AccountsProvider, LoginMethod
from tai42_contract.accounts.registry import register_accounts_provider


class FixtureAccountsProvider(AccountsProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    def login_methods(self) -> list[LoginMethod]:
        return []

    async def needs_bootstrap(self) -> bool:
        return False

    async def revoke_session(self, token: str) -> bool:
        return False


register_accounts_provider("fixture-accounts", FixtureAccountsProvider)
