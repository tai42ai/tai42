"""The provider door's service: an MCP server added by its address becomes a discovered provider.

A provider added here is a discovered OAuth descriptor written into the manifest ``connectors`` list
through :meth:`~tai42_skeleton.config.service.ConfigService.apply_change` (validate, persist, reload,
fleet broadcast); :func:`add_provider` / :func:`remove_provider` are the in-place mutators that
pipeline runs. An id is free only when nothing is stored under it — no descriptor in the manifest, no
connection record (live or lapsed), no client row — so a new provider never inherits a connection or a
credential made under a departed one. The manifest, the token store and the provider registry are
reached through the ``connection_service`` package namespace, the one place their test doubles bind.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Container, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from tai42_contract.connectors.models import AddServerRequest
from tai42_contract.connectors.providers import McpServerDescriptor, ProviderDescriptor, SubServiceDescriptor
from tai42_kit.db import component_store_configured
from tai42_kit.net import is_loopback_host

from tai42_skeleton.connectors.oauth.discovery import DiscoveredAuthorization
from tai42_skeleton.connectors.providers.registry import SEED_CATEGORY_IDS
from tai42_skeleton.connectors.service import connection_service as _svc
from tai42_skeleton.connectors.store import client_store
from tai42_skeleton.connectors.store.redis_pg import RedisPgConnectorTokenStore, StoredConnection
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.marketplace.provides import connector_provider_ids
from tai42_skeleton.marketplace.resolve import spec_from_row
from tai42_skeleton.marketplace.store import InstallRecord, MarketplaceInstallStore

logger = logging.getLogger(__name__)

#: The longest id the suggestion produces.
SUGGESTED_ID_MAX = 64

#: The single sub-service of a provider added by its address.
SUB_SERVICE_ID = "default"

#: The category of a provider added without one.
DEFAULT_CATEGORY = "other"

ClientSource = Literal["env", "manual", "dynamic", "unset"]

_NOT_SLUG = re.compile(r"[^a-z0-9]+")


class ProviderIdTakenError(ValueError):
    """A provider is added under an id the manifest's ``connectors`` list already holds."""


# -- the address ---------------------------------------------------------------------------------


def check_server_url(url: str) -> str:
    """Return ``url`` when it may be looked up and added, else raise ``ValueError`` naming why.

    An ``https`` URL, or ``http`` on a loopback host, with a host, no embedded credentials, no
    fragment and no whitespace or control character.
    """
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        raise ValueError("the server address must be one line with no whitespace or control characters")
    try:
        split = urlsplit(url)
        host = split.hostname
    except ValueError as exc:
        raise ValueError(f"the server address is not a URL: {exc}") from exc
    if split.scheme not in ("https", "http"):
        raise ValueError("the server address must be an https URL (http only on a loopback host)")
    if not host:
        raise ValueError("the server address must name a host")
    if "@" in split.netloc:
        raise ValueError("the server address must not carry credentials")
    if split.fragment or url.endswith("#"):
        raise ValueError("the server address must not carry a fragment")
    if split.scheme == "http" and not is_loopback_host(host):
        raise ValueError("the server address must be an https URL (http only on a loopback host)")
    return url


# -- the suggestion and the descriptor -----------------------------------------------------------


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def suggest_provider_id(url: str, taken: Container[str]) -> str:
    """A free provider id for the server at ``url``: its host as a slug, numbered past every id in ``taken``.

    The host lower-cased, every run of characters outside ``[a-z0-9]`` turned into ``_``, a leading
    non-letter prefixed with ``mcp_``, cut to :data:`SUGGESTED_ID_MAX`; then ``_2``, ``_3``, … (the
    base cut to keep the whole within the limit) until the id is not in ``taken``.
    """
    base = _NOT_SLUG.sub("_", _host(url))
    if not base[:1].isalpha():
        # The separator a leading run already became is kept as the prefix's own.
        base = f"mcp{base}" if base.startswith("_") else f"mcp_{base}"
    base = base[:SUGGESTED_ID_MAX]
    if base not in taken:
        return base
    number = 2
    while True:
        suffix = f"_{number}"
        candidate = f"{base[: SUGGESTED_ID_MAX - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
        number += 1


def suggested_display_name(url: str, discovered: DiscoveredAuthorization) -> str:
    """The name a provider added from ``url`` takes when none is given: the resource's own name, else the host."""
    return discovered.resource_name or _host(url)


def build_descriptor(
    request: AddServerRequest, discovered: DiscoveredAuthorization, provider_id: str
) -> ProviderDescriptor:
    """The discovered provider descriptor for the server ``request`` names, under ``provider_id``.

    A stored-client provider (no ``oauth`` block, no env names, no issuer pin) of origin
    ``community`` with one sub-service :data:`SUB_SERVICE_ID` on the server's address. Raises
    ``ValueError`` for a category that is not seeded or a descriptor that does not validate.
    """
    category = request.category or DEFAULT_CATEGORY
    if category not in SEED_CATEGORY_IDS:
        raise ValueError(f"category {category!r} is not one of {', '.join(SEED_CATEGORY_IDS)}")
    display_name = request.display_name or suggested_display_name(request.url, discovered)
    return ProviderDescriptor(
        id=provider_id,
        display_name=display_name,
        description=request.description,
        icon_url=request.icon_url,
        kind="oauth",
        origin="community",
        category=category,
        sub_services={
            SUB_SERVICE_ID: SubServiceDescriptor(
                id=SUB_SERVICE_ID,
                display_name=display_name,
                scopes=list(request.scopes),
                mcp_server=McpServerDescriptor(type="http", url=request.url),
            )
        },
    )


# -- the manifest mutators -----------------------------------------------------------------------


def _connectors(document: dict[str, Any]) -> list[Any]:
    entries = document.get("connectors")
    return entries if isinstance(entries, list) else []


def manifest_provider_ids(document: dict[str, Any]) -> list[str]:
    """The ids of the providers in the manifest ``document``'s ``connectors`` list."""
    return [entry["id"] for entry in _connectors(document) if isinstance(entry, dict) and "id" in entry]


def add_provider(document: dict[str, Any], descriptor: ProviderDescriptor) -> None:
    """Append ``descriptor`` to the preserved manifest ``document``'s ``connectors`` list in place.

    Raises :class:`ProviderIdTakenError` when the list already holds its id.
    """
    if descriptor.id in manifest_provider_ids(document):
        raise ProviderIdTakenError(f"a provider with id {descriptor.id!r} is already in the manifest")
    document["connectors"] = [*_connectors(document), descriptor.model_dump(mode="json", exclude_none=True)]


def remove_provider(document: dict[str, Any], provider_id: str) -> None:
    """Remove the provider ``provider_id`` from the preserved manifest ``document`` in place; ``KeyError`` if absent."""
    entries = _connectors(document)
    kept = [entry for entry in entries if not (isinstance(entry, dict) and entry.get("id") == provider_id)]
    if len(kept) == len(entries):
        raise KeyError(provider_id)
    document["connectors"] = kept


def _manifest_ids() -> list[str]:
    """The provider ids in the persisted manifest (what an add would collide with)."""
    return manifest_provider_ids(_svc.ConfigService.from_app().read_manifest_document())


def _live_manifest_ids() -> set[str]:
    """The provider ids in the live in-process manifest; an in-memory read."""
    from tai42_skeleton.app import instance

    return {descriptor.id for descriptor in instance.app.admin.live_manifest_typed.connectors}


# -- removable -----------------------------------------------------------------------------------


async def _installed_plugins() -> list[InstallRecord]:
    return await MarketplaceInstallStore().list_installed()


async def plugin_connector_owners() -> dict[str, str]:
    """``{provider_id: plugin ref}`` for every connector an installed plugin provides."""
    owners: dict[str, str] = {}
    for record in await _installed_plugins():
        for provider_id in connector_provider_ids(spec_from_row(record)):
            owners.setdefault(provider_id, record.ref)
    return owners


def _stored_client_provider(descriptor: ProviderDescriptor) -> bool:
    return descriptor.is_discovered and not descriptor.client_id_env


def _refusal(descriptor: ProviderDescriptor, manifest_ids: Container[str], owners: dict[str, str]) -> str | None:
    provider_id = descriptor.id
    owner = owners.get(provider_id)
    if owner is not None:
        return (
            f"provider {provider_id!r} is provided by the installed plugin {owner!r}; uninstall the plugin to remove it"
        )
    if not _stored_client_provider(descriptor):
        return (
            f"provider {provider_id!r} is declared with its own sign-in settings; "
            "it is managed in the manifest's connectors list"
        )
    if provider_id not in manifest_ids:
        return f"provider {provider_id!r} is not in the manifest's connectors list"
    return None


async def removal_refusal(descriptor: ProviderDescriptor) -> str | None:
    """Why the provider door may not remove ``descriptor`` (naming where it is managed), or ``None`` when it may.

    Removable: a discovered provider with a stored client (no env names) that is in the live
    manifest's ``connectors`` list and that no installed plugin provides.
    """
    return _refusal(descriptor, _live_manifest_ids(), await plugin_connector_owners())


async def provider_is_removable(provider_id: str) -> bool:
    """Whether the provider door may remove the registered provider ``provider_id`` (``False`` when unknown)."""
    try:
        descriptor = _svc.get_provider(provider_id)
    except KeyError:
        return False
    return await removal_refusal(descriptor) is None


async def removable_ids(descriptors: Sequence[ProviderDescriptor]) -> set[str]:
    """The ids of the ``descriptors`` the door may remove, read with one plugin-index query.

    Empty when the store is not configured (the door's operations then refuse).
    """
    if not component_store_configured(SKELETON_COMPONENT):
        return set()
    candidates = [d for d in descriptors if _stored_client_provider(d)]
    if not candidates:
        return set()
    manifest_ids = _live_manifest_ids()
    owners = await plugin_connector_owners()
    return {d.id for d in candidates if _refusal(d, manifest_ids, owners) is None}


# -- where a client comes from -------------------------------------------------------------------


async def client_sources(descriptors: Iterable[ProviderDescriptor]) -> dict[str, ClientSource | None]:
    """Where each provider's sign-in client comes from, with one grouped client-store query.

    ``env`` when the descriptor names env variables; for a stored-client provider what the store
    holds — ``manual``, else a usable ``dynamic`` row, else ``unset`` (also with the store not
    configured); ``None`` for a no-auth provider. A provider whose logins use the client-identity
    document stores nothing and reads ``unset``.
    """
    out: dict[str, ClientSource | None] = {}
    stored: list[str] = []
    for descriptor in descriptors:
        if descriptor.kind != "oauth":
            out[descriptor.id] = None
        elif descriptor.client_id_env:
            out[descriptor.id] = "env"
        else:
            out[descriptor.id] = "unset"
            stored.append(descriptor.id)
    if stored and component_store_configured(SKELETON_COMPONENT):
        out.update(await client_store.active_client_store().sources(stored))
    return out


# -- connections and free ids --------------------------------------------------------------------


async def _stored_connections() -> list[StoredConnection]:
    return await RedisPgConnectorTokenStore().list_all_including_expired()


def _lapsed(row: StoredConnection, now: datetime) -> bool:
    expires = row.session_expires_at
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= now


async def connections_of(provider_id: str) -> tuple[list[str], list[str]]:
    """``(live, lapsed)`` connection ids of ``provider_id``, read from the durable table, lapsed sessions included."""
    now = datetime.now(UTC)
    live: list[str] = []
    lapsed: list[str] = []
    for row in await _stored_connections():
        if row.provider_id == provider_id:
            (lapsed if _lapsed(row, now) else live).append(row.connection_id)
    return live, lapsed


async def taken_provider_ids() -> set[str]:
    """Every id that is not free: in the manifest, or with a connection record, or with a client row."""
    taken = set(_manifest_ids())
    taken.update(row.provider_id for row in await _stored_connections())
    taken.update(row.provider_id for row in await client_store.active_client_store().enumerate_rows())
    return taken


async def id_taken_reason(provider_id: str) -> str | None:
    """Why ``provider_id`` is not free for a new provider, or ``None`` when it is."""
    if provider_id in _manifest_ids():
        return "a provider with this id is in the manifest"
    live, lapsed = await connections_of(provider_id)
    if live or lapsed:
        return f"a connection record is still stored under this id ({len(live)} live, {len(lapsed)} lapsed)"
    if await client_store.active_client_store().has_rows(provider_id):
        return "client credentials are still stored under this id"
    return None
