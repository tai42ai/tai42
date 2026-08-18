"""A fixture distribution shipping TWO route modules under DISTINCT mount bindings.

Models the accounts-postgres shape: ``routes_login`` (base ``login``) and
``routes_users`` (base ``auth``) live in ONE distribution but bind separately, and
each captures its mount at import (via ``register_route``, which raises off a
binding). A reload MUST re-fire each module under ITS OWN binding only — never
blanket-reload the distribution, which would re-run the sibling module under the
wrong binding (or none).
"""
