"""Schema identity of the plugin's own chain.

The plugin owns a migration chain — ``migrations/0001_baseline.sql`` and any later
files, declared by ``migrations: migrations`` in its ``tai-plugin.yml`` — applied by
the shared runner (``tai db migrate``) and recorded in the per-database
``tai_schema_history`` table under the component name ``tai42-accounts-postgres`` (its
distribution name, the same identity the marketplace installer and the ``tai db`` CLI
stamp its rows with).

The platform's boot-time schema gate checks the chain whenever the manifest loads a
module of this plugin and the component's database is configured: a pending migration
or a checksum mismatch refuses the boot or reload naming ``tai db migrate``. The plugin
carries no gate of its own.
"""

from __future__ import annotations

from tai42_kit.db import component_store_configured

# The chain's identity in ``tai_schema_history`` — the plugin's distribution name
# (``spec.package``). Fixed once and forever; it never changes across releases.
COMPONENT = "tai42-accounts-postgres"

__all__ = ["COMPONENT", "accounts_store_configured"]


def accounts_store_configured() -> bool:
    """Whether this deployment configures the accounts store's database at all.

    Resolved through the central registry on the component's binding, read fresh so
    a config reload re-evaluates. The bound database is configured iff its password
    resolves to a non-empty value.
    """
    return component_store_configured(COMPONENT)
