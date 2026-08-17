"""The manifest LEAF: registers by importing its route-carrying sibling.

Importing this module runs the route registration purely as the ``import
route_sibling_plugin.inbound`` side-effect — the decorators live in the sibling,
not here — exactly as a real channel plugin's ``register`` module does.
"""

import route_sibling_plugin.inbound  # noqa: F401  (route registration side-effect)
