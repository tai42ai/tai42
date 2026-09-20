"""Connector provider registry — the engine's in-memory provider catalog.

The skeleton ships NO concrete provider. Registration is manifest-driven: each
``connectors`` entry in the manifest is registered through
``tai42_app.connectors.register_connector(descriptor)`` during boot/reload, which
forwards to :func:`tai42_skeleton.connectors.providers.registry.register_connector`.
"""
