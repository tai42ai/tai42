"""The epsilon fixture's router — the manifest LEAF (bumped version).

Identical role to the prior version: the module listed in the item's
``tai-plugin.yml`` and persisted into ``routers_modules``. It registers NOTHING
itself — it imports the route-carrying sibling ``_inbound`` purely for its
``@tai42_app.http.custom_route`` side-effect, so the routes live in a sibling of the
manifest leaf. The bumped sibling adds the public ``GET /probe`` route.
"""

from __future__ import annotations

import tai_e2e_market_epsilon._inbound  # noqa: F401  (route registration side-effect)
