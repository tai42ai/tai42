"""Client-input validators for the connection lifecycle.

Return-URL shape, sub-service membership and transport support, config-value presence, and scope derivation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from tai42_contract.connectors.providers import ProviderDescriptor

from tai42_skeleton.connectors.runtime.launch import SUPPORTED_MANAGED_TRANSPORTS, resolve_mcp_server

_RETURN_URL_RE = re.compile(r"^/[A-Za-z0-9_\-./?=&%]*\Z")


def _validate_return_url(value: str) -> str:
    # ``//evil.com`` is a protocol-relative URL the browser treats as absolute;
    # the regex permits a leading ``/`` so reject the ``//`` prefix explicitly to
    # close the open-redirect.
    if value.startswith("//") or not _RETURN_URL_RE.match(value):
        raise ValueError(f"return_url must be a same-origin path beginning with '/': {value!r}")
    return value


def _validate_sub_services(descriptor: ProviderDescriptor, sub_services: list[str]) -> None:
    if not sub_services:
        raise ValueError("enabled_sub_services must be non-empty")
    extra = set(sub_services) - set(descriptor.sub_services.keys())
    if extra:
        raise ValueError(f"unknown sub-services for provider {descriptor.id!r}: {sorted(extra)}")
    # Reject a sub-service on a transport managed calls cannot drive (e.g.
    # websocket) at connect time: it would probe healthy but raise on every real
    # tool call, so the user must never be able to complete a Connect for it.
    for sub_id in sub_services:
        server = resolve_mcp_server(descriptor, sub_id)
        if server.type not in SUPPORTED_MANAGED_TRANSPORTS:
            raise ValueError(
                f"sub-service {sub_id!r} of provider {descriptor.id!r} uses transport "
                f"{server.type!r}, which connector-managed calls do not support "
                f"(supported: {sorted(SUPPORTED_MANAGED_TRANSPORTS)})"
            )


def _validate_config_values(descriptor: ProviderDescriptor, config_values: dict[str, str]) -> None:
    """Validate client-supplied config_values against the provider's config_fields.

    Raises on an unknown key or a missing/empty required value — never silently
    drops or defaults.
    """
    allowed = {field.key for field in descriptor.config_fields}
    unknown = set(config_values) - allowed
    if unknown:
        raise ValueError(f"unknown config values for provider {descriptor.id!r}: {sorted(unknown)}")
    for field in descriptor.config_fields:
        if field.required and not config_values.get(field.key):
            raise ValueError(f"missing required config value {field.key!r} for provider {descriptor.id!r}")


def _scopes_for(descriptor: ProviderDescriptor, sub_services: Iterable[str]) -> list[str]:
    return sorted({scope for sub_id in sub_services for scope in descriptor.sub_services[sub_id].scopes})
