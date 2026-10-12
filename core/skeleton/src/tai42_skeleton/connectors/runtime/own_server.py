"""A connection's credential goes only to the server its provider defines.

The pure comparison both the run-time check and the managed-entry repair use, so what the check
refuses and what the repair rewrites cannot drift.
"""

from __future__ import annotations

from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.manifest import TaiMCPConfig

from tai42_skeleton.connectors.runtime.launch import resolve_mcp_server
from tai42_skeleton.manifest.transport import detect_transport


def own_server_refusal(config: TaiMCPConfig, descriptor: ProviderDescriptor | None) -> str | None:
    """Why the managed entry ``config`` must not receive its connection's credential, or ``None``.

    An entry that talks to a ``url`` (the transport its request follows: which field is set, never its
    free-text ``type``) must carry, exactly, the ``url`` ``descriptor`` defines for the referenced
    sub-service — the value the managed-entry builder copies verbatim. Any other entry is not checked.
    ``descriptor`` is ``None`` when the referenced provider is not registered.
    """
    ref = config.managed
    if ref is None:
        raise RuntimeError(f"config {config.title!r} is not connector-managed")
    if detect_transport(config.config) != "http":
        return None
    if descriptor is None:
        return f"provider {ref.provider_id!r} is not registered"
    if ref.sub_service not in descriptor.sub_services:
        return f"provider {ref.provider_id!r} has no sub-service {ref.sub_service!r}"
    server = resolve_mcp_server(descriptor, ref.sub_service)
    if server.url is None:
        return f"provider {ref.provider_id!r} defines no http server for sub-service {ref.sub_service!r}"
    if server.url != config.config.url:
        return f"its url is not the one provider {ref.provider_id!r} defines for sub-service {ref.sub_service!r}"
    return None
