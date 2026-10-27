"""Response models for the ``@operation`` success bodies, one module per operation domain.

Each model DESCRIBES the inner payload its operation returns; the route adapter wraps that
payload in the ``{"data": ...}`` success envelope, so no model re-declares the envelope or
reshapes a wire body. Bare-list and dynamic-map bodies are NAMED ``RootModel`` subclasses (the
offline emitter registers ``components.schemas`` by ``model.__name__``, so a named class yields
a stable, unique component). A genuinely-open sub-field (arbitrary per-deployment JSON, not a
fixed platform shape) is typed ``JsonValue``. Shared cross-seam models (``ApplyResponse``,
``FanoutSummary``, ``ProfileApplyResponse``, ``OpaqueJson``) live in
:mod:`tai42_contract.app.responses` and existing record models are imported and reused, never
redefined here. The bodies several domains return live in :mod:`.shared`.
"""
