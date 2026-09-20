"""The epsilon fixture's router — the manifest LEAF.

This module is the one listed in the item's ``tai-plugin.yml`` (and persisted into
``routers_modules``). It registers NOTHING itself: it imports the route-carrying
sibling ``_inbound`` purely for its ``@tai42_app.http.custom_route`` side-effect, so
the fixture reproduces the channel shape where the routes live in a sibling of the
manifest leaf. The skeleton installer's manifest patch persists this module (before
the SPA catch-all) and the epoch build mounts the sibling's routes into the served
ASGI app; a plain in-process config reload must re-fire them.
"""

from __future__ import annotations

import tai_e2e_market_epsilon._inbound  # noqa: F401  (route registration side-effect)
