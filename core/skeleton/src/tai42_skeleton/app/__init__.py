"""The app facade package: ``TaiMCP`` (the concrete ``tai42_contract.app.TaiApp`` impl) and its lifecycle/facets.

Nothing is re-exported here, so importing any ``tai42_skeleton.app`` submodule
(the route registry, the route shapes, a facet) loads that submodule alone and
never the server. ``TaiMCP`` lives in :mod:`tai42_skeleton.app.server`; the
process singleton lives in :mod:`tai42_skeleton.app.instance` (imported
explicitly so the heavy app object is built only when a launcher wants it).
"""
