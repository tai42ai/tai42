"""Provisioning surface over the access-control policy store.

The auth gate (``policy``/``verifier`` + the identity provider) is the read side:
it resolves an inbound key to an identity, loads that identity's policy, and maps a
request path to a scope id. This module is the write side's CRUD an operator UI drives
to wire routes to scopes, read and restore policies, and manage principal rows; the
api-key mint / edit / revoke orchestration is
:mod:`~tai42_skeleton.access_control.api_key_lifecycle`.

Two backends are reached here, each owning a distinct slice of state:

- The POLICY RULES (scopes, route/pattern mappings, per-user policy bodies, principal
  rows) live in Postgres — the
  :class:`~tai42_skeleton.access_control.store.PostgresAccessControlStore` this module
  delegates every policy op to. It is the ONLY policy store. The plain-Redis
  ``ac:policy_version`` cache-buster is bumped through
  :mod:`~tai42_skeleton.access_control.policy_version` by every writer here after its
  committed change (once per
  :func:`~tai42_skeleton.access_control.policy_version.policy_write_batch` inside one), so
  no caller has to remember the invalidation.
- The api-key IDENTITY records are OWNED by the active identity provider plugin, reached
  ONLY through the ``ApiKeyIdentityProvider`` API resolved through the module-level
  registry the runtime auth adapter uses — this module never imports the plugin nor
  touches ``ac:key:*``. It resolves the mint-capable provider and reads the identity
  enumeration to classify and list provisioned keys.

Backend errors are never swallowed — they propagate so a failed provisioning op is
loud.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM
from tai42_contract.access_control.identity import ApiKeyIdentityProvider, IdentityProvider
from tai42_kit.access_control.registry import get_identity_provider_factory

from tai42_skeleton.access_control import policy_version
from tai42_skeleton.access_control.settings import AccessControlSettings, access_control_settings
from tai42_skeleton.access_control.store import access_control_store
from tai42_skeleton.template.references import assert_templates_stored
from tai42_skeleton.template.stored_refs import condition_template_refs

# Identities that are infrastructure, not provisioned end-user keys, and so are
# omitted from the enumerated tokens payload.
_RESERVED_USER_IDS = frozenset({"__root__"})


def is_minted_policy(body: Mapping[str, Any]) -> bool:
    """Whether a policy ``body`` was minted as an api key.

    The single marker is a NON-NULL :data:`KEY_FINGERPRINT_CLAIM` value in
    ``policy_data``: every mint stamps it and every edit/rollback carries it, while a
    role-assigned account row never has one. A JSON ``null`` under the claim does not
    count as minted — the value must be present and non-null, matching the store's
    ``list_minted_policies`` selection.
    """
    return (body.get("policy_data") or {}).get(KEY_FINGERPRINT_CLAIM) is not None


def _settings() -> AccessControlSettings:
    return access_control_settings()


def _resolve_provider(name: str) -> IdentityProvider:
    """Build the provider named ``name`` through the module-level registry.

    The SAME path the runtime auth adapter uses (never a direct import of the plugin). An
    unregistered name raises LOUDLY (``KeyError`` out of the registry).
    """
    return get_identity_provider_factory(name)(_settings())


def _identity_provider() -> ApiKeyIdentityProvider:
    """Resolve the FIRST mint-capable identity provider in the configured chain.

    Walks ``auth_providers`` in order and returns the first that implements
    ``ApiKeyIdentityProvider`` — the provider owns the identity record; this surface
    orchestrates it for the record half and delegates the policy half to the PG store.

    An unregistered provider name raises LOUDLY (``KeyError`` out of the registry) — a
    management op must never run when access control resolves no identity provider.
    When NO configured provider is mint-capable (a validator-only deployment), raise
    ``TypeError`` naming the chain — the loud behavior the capabilities route surfaces
    ahead of a mint attempt rather than a raw 500 at mint time.
    """
    s = _settings()
    chain = s.resolved_auth_providers()
    for name in chain:
        provider = _resolve_provider(name)
        if isinstance(provider, ApiKeyIdentityProvider):
            return provider
    raise TypeError(
        f"no configured identity provider {chain!r} implements ApiKeyIdentityProvider; "
        "the api-key provisioning surface requires a key-minting provider"
    )


def provider_capabilities() -> list[tuple[str, bool]]:
    """Each configured provider as ``(name, mintable)``, ``mintable`` iff it implements ``ApiKeyIdentityProvider``.

    The capabilities route surfaces this so a validator-only deployment disables its mint UI
    instead of erroring at mint time.
    """
    return [
        (name, isinstance(_resolve_provider(name), ApiKeyIdentityProvider))
        for name in _settings().resolved_auth_providers()
    ]


# -- scope / route reads (delegated to the PG store) -------------------------


async def get_all_existing_scopes() -> dict[str, str]:
    """Every non-public route mapping as ``{url: scope_id}``."""
    return await access_control_store().get_all_existing_scopes()


async def get_all_route_mappings() -> dict[str, str]:
    """Every route mapping as ``{url: value}``, INCLUDING public routes whose value is the public marker.

    ``get_all_existing_scopes`` filters that marker out. This is the full, faithful set a backup
    needs so an explicit public mapping round-trips instead of silently reverting to protected on
    restore.
    """
    return await access_control_store().get_all_route_mappings()


async def get_all_existing_patterns() -> dict[str, str]:
    """Every dynamic route's ``{url: pattern}`` mapping."""
    return await access_control_store().get_all_existing_patterns()


async def get_all_existing_tokens_payload() -> list[dict[str, Any]]:
    """Every provisioned key's identity merged with its policy — an ORCHESTRATION, not a single store read.

    The identities (``user_id``/``description``) come from the active provider's
    ``list_identities`` enumeration (it owns the identity records); each is merged
    with its policy read from the PG store and carries ``orphaned: False``.
    Infrastructure identities (falsy or reserved ``user_id``, e.g. ``__root__``) are
    skipped; policy fields win on merge. Key material is never included — only the
    stored ``user_id``/``description`` plus policy fields.

    A minted policy row (one carrying the key fingerprint) whose identity record is
    gone is an ORPHAN: it is appended with ``orphaned: True`` and an empty
    ``description`` (its only home was the lost identity record), so an operator sees
    a partial-restore state rather than a silently missing key. A never-minted
    role-assigned account row (no fingerprint) is not enumerated here.

    Ownership rides the policy merge: the mint path dual-homes the owner claim into
    ``policy_data`` under ``OWNER_USER_ID_CLAIM`` (its management/listing home), so an
    owned key's ``owner_user_id`` surfaces here for the route layer to filter on.

    Returns ``[]`` on a validator-only deployment (no mint-capable provider in the
    chain): with no key-minting provider there are no provisioned api-keys, so the
    empty enumeration is the accurate answer rather than an error.
    """
    # No mint-capable provider means no provisioned api-keys to enumerate — the accurate
    # empty answer, so this stays clear of ``_identity_provider()`` (which raises when no
    # provider mints). The genuine mint path
    # (``api_key_lifecycle.add_user_api_key``) still raises loudly.
    if not any(mintable for _name, mintable in provider_capabilities()):
        return []
    provider = _identity_provider()
    store = access_control_store()
    identities = await provider.list_identities()
    seen: set[str] = set()
    payload: list[dict[str, Any]] = []
    for user_id, description in identities:
        if not user_id or user_id in _RESERVED_USER_IDS:
            continue
        seen.add(user_id)
        merged: dict[str, Any] = {"user_id": user_id, "description": description}
        policy = await store.get_policy_body(user_id)
        if policy:
            merged.update(policy)
        merged["orphaned"] = False
        payload.append(merged)
    # Minted policy rows with no matching identity record are orphans (their principal
    # is gone): surfaced flagged, description-less, reserved ids excluded.
    for user_id, policy in await store.list_minted_policies():
        if user_id in seen or user_id in _RESERVED_USER_IDS:
            continue
        payload.append({"user_id": user_id, "description": "", **policy, "orphaned": True})
    return payload


async def _has_identity_record(provider: ApiKeyIdentityProvider, user_id: str) -> bool:
    """Whether the provider still holds an identity record for ``user_id``.

    The enumeration is the provider API's only NON-destructive existence read (``revoke``
    answers by destroying the record). A provider fault propagates: a revoke must never
    proceed on a guessed existence.
    """
    return any(uid == user_id for uid, _description in await provider.list_identities())


async def api_key_state(user_id: str) -> Literal["absent", "live", "orphaned", "account"]:
    """The provisioning state of ``user_id`` across the policy store and identity provider.

    - ``absent`` — no policy row;
    - ``live`` — a policy row AND a live identity record;
    - ``orphaned`` — a policy row minted as an api key (it carries the key fingerprint)
      whose identity record is gone;
    - ``account`` — a policy row with no fingerprint and no identity record (a
      role-assigned account user, never minted as an api key).

    The classification the import and mint paths read to choose skip / re-mint / mint:
    one policy read plus (only when a row exists) one provider enumeration. Revoke uses
    its own identity-first read instead, so a half-finished revoke stays retriable.
    """
    store = access_control_store()
    body = await store.get_policy_body(user_id)
    if body is None:
        return "absent"
    provider = _identity_provider()
    if await _has_identity_record(provider, user_id):
        return "live"
    if is_minted_policy(body):
        return "orphaned"
    return "account"


# -- scope / route mutations (delegated to the PG store) ---------------------


async def add_url_to_scope(scope_id: str, url: str, pattern: str | None = None) -> None:
    """Map ``url`` to ``scope_id`` (optionally with a dynamic ``pattern``); the policy cache is invalidated.

    Mapping a url to the universal grant ``"*"`` raises ``ValueError`` with nothing written, as
    does mapping a url a route public by its own declaration serves: such a route answers
    everyone whatever its row says, so a scope mapping would read as protecting it while it
    stays open.
    """
    from tai42_skeleton.access_control.path_canon import MalformedPathError, canonicalize_path
    from tai42_skeleton.access_control.verifier import url_is_declared_public

    try:
        canonical = canonicalize_path(url)
    except MalformedPathError:
        canonical = None
    if canonical is not None and url_is_declared_public(canonical, _settings()):
        raise ValueError(
            f"{url!r} is public by its own declaration and stays open to everyone whatever scope it is "
            f"mapped to, so it cannot be mapped into scope {scope_id!r}"
        )
    await access_control_store().add_url_to_scope(scope_id, url, pattern)
    await policy_version.record_policy_change()


async def remove_url_from_scope(url: str) -> tuple[bool, list[tuple[str, dict[str, Any]]]]:
    """Unmap ``url``, cascading its scope out of every token policy when the scope loses its last url.

    Returns ``(existed, [(user_id, committed_body), …])``; the policy cache is invalidated when the
    url existed.
    """
    existed, affected = await access_control_store().remove_url_from_scope(url)
    if existed:
        await policy_version.record_policy_change()
    return existed, affected


async def remove_scope(scope_id: str) -> tuple[int, list[tuple[str, dict[str, Any]]]]:
    """Delete a scope, stripping it from every token policy and deleting its routes.

    Returns ``(deleted_count, [(user_id, committed_body), …])``; the policy cache is invalidated
    when anything was deleted. Removing the public marker or the universal grant ``"*"``
    raises ``ValueError`` with nothing written.
    """
    deleted, affected = await access_control_store().remove_scope(scope_id)
    if deleted > 0:
        await policy_version.record_policy_change()
    return deleted, affected


async def get_public_route_pins() -> list[str]:
    """The sorted urls pinned to the public marker."""
    return await access_control_store().get_public_route_pins()


async def pin_route_public(url: str, pattern: str | None = None) -> None:
    """Pin ``url`` public (optionally with a dynamic ``pattern``), re-pointing it off any prior scope.

    The dedicated public-pin writer — the marker never routes through ``add_url_to_scope``. The
    policy cache is invalidated.
    """
    await access_control_store().pin_route_public(url, pattern)
    await policy_version.record_policy_change()


async def unpin_public_route(url: str) -> bool:
    """Unpin a public ``url``. Returns ``False`` when it was not pinned public; else the policy cache is invalidated."""
    unpinned = await access_control_store().unpin_public_route(url)
    if unpinned:
        await policy_version.record_policy_change()
    return unpinned


async def get_policy_body(user_id: str) -> dict[str, Any] | None:
    """The full policy record enforcement serves for ``user_id``, or ``None``."""
    return await access_control_store().get_policy_body(user_id)


# -- principals (delegated reads) --------------------------------------------


async def list_principals() -> list[dict[str, Any]]:
    """Every principal row, ordered by creation."""
    return await access_control_store().list_principals()


async def get_principal(user_id: str) -> dict[str, Any] | None:
    """The principal row for ``user_id``, or ``None`` when none exists."""
    return await access_control_store().get_principal(user_id)


async def any_principal_exists() -> bool:
    """Whether ANY principal exists — the ``needs_setup`` / setup-door 409 predicate."""
    return await access_control_store().any_principal_exists()


async def create_principal_row(user_id: str, kind: str, display_name: str, created_by: str | None) -> None:
    """Insert a principal row (the backup restore's create); the policy cache is invalidated.

    The store refuses a duplicate ``user_id`` with a loud ``ValueError``.
    """
    await access_control_store().create_principal(user_id, kind, display_name, created_by)
    await policy_version.record_policy_change()


async def delete_principal_row(user_id: str) -> None:
    """Delete a principal row whose policy never landed (the backup restore's compensation).

    The policy cache is invalidated.
    """
    await access_control_store().delete_principal(user_id)
    await policy_version.record_policy_change()


async def create_principal_policy(
    user_id: str, scopes: list[str], policy_data: dict[str, Any] | None, condition: dict[str, Any] | None
) -> None:
    """Insert a principal's own policy row (the backup restore's create); the policy cache is invalidated.

    A condition naming a stored template id the store lacks raises ``ValueError`` before the write.
    """
    await assert_templates_stored(condition_template_refs(condition), ValueError)
    await access_control_store().create_policy(user_id, scopes, policy_data, condition)
    await policy_version.record_policy_change()


async def set_principal_disabled_row(user_id: str, disabled: bool) -> None:
    """Flip a principal's ``disabled`` marker on both homes with no last-admin count; the policy cache is invalidated.

    The raw flip the backup restore applies to a recreated principal. The guarded flip every
    administrative door uses is :func:`~tai42_skeleton.access_control.roles.set_principal_disabled`.
    Raises ``KeyError`` when the principal or its policy row is absent.
    """
    await access_control_store().set_principal_disabled(user_id, disabled)
    await policy_version.record_policy_change()


async def restore_policy_body(user_id: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """Write a prior policy ``body`` back as the enforced policy — the store side of a version rollback.

    Returns the restored body, or ``None`` if ``user_id`` is not provisioned (a falsy sentinel the
    route's 404 guard tests). A restored body invalidates the policy cache. A body whose condition
    names a stored template id the store lacks raises ``ValueError`` before the write. A principal's
    own row is written under the last-admin guard: a body that demotes the last enabled admin
    principal raises :class:`~tai42_contract.accounts.errors.LastAdminError` with nothing written.
    """
    await assert_templates_stored(condition_template_refs(body.get("condition")), ValueError)
    store = access_control_store()
    if await store.get_principal(user_id) is not None:
        async with store.principal_guard_txn() as guard:
            restored = await guard.restore_policy_body(user_id, body)
    else:
        restored = await store.restore_policy_body(user_id, body)
    if restored is not None:
        await policy_version.record_policy_change()
    return restored
