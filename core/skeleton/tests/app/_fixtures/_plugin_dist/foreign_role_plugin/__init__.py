"""A fixture distribution modelling the accounts-postgres shape wired under a
NON-route role.

A manifest may list this ROOT package under a non-route role (a lifecycle/identity
entry). Importing the package runs its ``provider`` side-effect, and the package walk
also sweeps in the route submodule ``routes`` — which captures its mount base and
registers its route at import. Under a foreign role the walk carries no binding, so
each mapped route submodule must be bound from the mount map by its OWN name.
"""

from foreign_role_plugin import provider  # noqa: F401  (lifecycle-role side-effect)
