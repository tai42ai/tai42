"""A fixture distribution whose manifest leaf BOTH registers a route ITSELF and
imports a route-carrying sibling.

Models a plugin whose leaf serves one route inline and pulls a sibling for a second
route's ``@custom_route`` side-effect. The owner records TWO route modules (the leaf
and the sibling), so its reload extras are the sibling alone — and a rebuild must
re-fire both under the one binding, keeping both routes.
"""
