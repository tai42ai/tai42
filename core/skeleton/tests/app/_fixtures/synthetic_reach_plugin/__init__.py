"""A made-up plugin whose route modules declare route reach, as any plugin's would.

Its two route items, as a plugin manifest declares them (:data:`ROUTE_ITEMS`):

* ``self-service`` (mount base ``auth``, :mod:`.self_service_routes`) — ``GET``/``PUT``
  :data:`SELF_SERVICE_TEMPLATE`, a templated caller self-service surface under the
  ``/api/auth`` control plane (``self_service=True``);
* ``doors`` (mount base ``synthetic``, :mod:`.door_routes`) — ``GET``
  :data:`ANY_AUTHENTICATED_PATH`, an identity-introspection surface any authenticated
  identity reaches (``any_authenticated=True``), and ``POST`` :data:`PRE_AUTH_PATH`, a public
  pre-authentication surface that ignores a presented credential (``pre_auth=True``).

Importing this package registers nothing. A test imports each route module under its item's
mount binding (:func:`~tai42_skeleton.app.mount_map.bind_module`), as the plugin loader does,
so every route is recorded plugin-owned at its mounted absolute path.
"""

from __future__ import annotations

from tai42_contract.plugins import RouteDecl

from tai42_skeleton.app.mount_map import MountBinding

OWNER_REF = "acme/synthetic"

# The mounted absolute paths the routes serve.
SELF_SERVICE_TEMPLATE = "/api/auth/synthetic/{item}"
ANY_AUTHENTICATED_PATH = "/api/synthetic/identity"
PRE_AUTH_PATH = "/api/synthetic/entry/exchange"

# (route module, its item's mount binding), one per declared route item.
ROUTE_ITEMS: tuple[tuple[str, MountBinding], ...] = (
    (
        f"{__name__}.self_service_routes",
        MountBinding(
            OWNER_REF,
            "self-service",
            "auth",
            (
                RouteDecl(path="/synthetic/{item}", methods=["GET"], public=False),
                RouteDecl(path="/synthetic/{item}", methods=["PUT"], public=False),
            ),
        ),
    ),
    (
        f"{__name__}.door_routes",
        MountBinding(
            OWNER_REF,
            "doors",
            "synthetic",
            (
                RouteDecl(path="/identity", methods=["GET"], public=False),
                RouteDecl(path="/entry/exchange", methods=["POST"], public=True),
            ),
        ),
    ),
)
