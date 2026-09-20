"""The manifest LEAF: registers by importing the route-carrying sibling.

This module — the one a manifest names and the mount map binds — registers NOTHING
itself. It imports the sibling ``inbound`` purely for its route-registration
side-effect, so re-importing this leaf alone leaves ``inbound`` cached in
``sys.modules`` and its route never re-fires.
"""

import route_sibling_plugin.inbound  # noqa: F401  (route registration side-effect)
