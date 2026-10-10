"""Provisioning surface over the access-control policy store.

The auth gate (``policy``/``verifier`` + the identity provider) is the read side:
it resolves an inbound key to an identity, loads that identity's policy, and maps a
request path to a scope id. This module is the write side — the CRUD an operator
UI drives to mint keys, edit policies, and wire routes to scopes.

Three backends are orchestrated here, each owning a distinct slice of state:

- The POLICY RULES (scopes, route/pattern mappings, per-user policy bodies) live in
  Postgres — the :class:`~tai42_skeleton.access_control.store.PostgresAccessControlStore`
  this module delegates every policy op to. It is the ONLY policy store.
- The api-key IDENTITY record (key hash → ``{user_id, description}`` plus its
  ``user_id`` → hash reverse lookup) is OWNED by the active identity provider plugin, reached ONLY through
  the ``ApiKeyIdentityProvider`` API (``provision``/``revoke``/``update_description``/
  ``list_identities``) resolved through the module-level registry the runtime auth
  adapter uses — this module never imports the plugin nor touches ``ac:key:*``.
- The ``ac:context:{user_id}`` per-user LIVE COUNTERS are a plain Redis HASH on the
  AC Redis, created by the first counter write (external metering writers use
  ``HSET``/``HINCRBY`` with JSON-encoded values) and deleted at revoke here; the
  plain-Redis ``ac:policy_version`` cache-buster is bumped by every writer here after its
  committed change (once per :func:`policy_write_batch` inside one), so no caller has to
  remember the invalidation.

Backend errors are never swallowed — they propagate so a failed provisioning op is
loud. The mint/revoke orchestration is fail-closed (identity record first on mint,
policy row first on revoke); a failure mid-way raises loudly rather than leaving a
silent orphan, and every step is ordered so a plain retry finishes the job.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM, UNIVERSAL_SCOPE
from tai42_contract.access_control.identity import ApiKeyIdentityProvider, IdentityProvider
from tai42_contract.template import TemplatedText
from tai42_kit.access_control.registry import get_identity_provider_factory
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.policy_version_scope import remember_policy_version
from tai42_skeleton.access_control.settings import AccessControlSettings, access_control_settings
from tai42_skeleton.access_control.store import access_control_store, refuse_disabled_claim_at_mint
from tai42_skeleton.utils.redis_typing import awaited

logger = logging.getLogger(__name__)

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


class _Unset(Enum):
    """Sentinel marking an ``edit_user_payload`` argument the caller did not supply.

    A partial edit leaves such a field at its stored value; only fields given an explicit value
    (including ``None``/``{}``/``""``) are overwritten, so editing one field never silently
    clears another.
    """

    UNSET = "unset"


_UNSET = _Unset.UNSET


def _settings() -> AccessControlSettings:
    return access_control_settings()


@dataclass
class _Batch:
    changed: bool = False


_policy_write_batch: ContextVar[_Batch | None] = ContextVar("policy_write_batch", default=None)


@asynccontextmanager
async def policy_write_batch() -> AsyncIterator[None]:
    """Run a run of policy writes under ONE cache invalidation, bumped on exit if anything changed.

    Inside the block every writer of this module records its change instead of bumping; the
    one bump runs in a ``finally``, so a raise part-way still invalidates what was written.
    A revoke's bump is not deferred (its fail-closed order needs it immediately). Nested use
    raises ``RuntimeError``.
    """
    if _policy_write_batch.get() is not None:
        raise RuntimeError("policy_write_batch is not reentrant")
    batch = _Batch()
    token = _policy_write_batch.set(batch)
    try:
        yield
    finally:
        _policy_write_batch.reset(token)
        if batch.changed:
            await bump_policy_version()


async def record_policy_change() -> None:
    """Invalidate the policy cache after a committed enforced-authority change.

    Bumps the policy version now, or — inside a :func:`policy_write_batch` — once when the
    batch ends.
    """
    batch = _policy_write_batch.get()
    if batch is not None:
        batch.changed = True
        return
    await bump_policy_version()


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


def _context_key(s: AccessControlSettings, user_id: str) -> str:
    return f"{s.context_prefix}{user_id}"


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
    # provider mints). The genuine mint path (``add_user_api_key``) still raises loudly.
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
    await record_policy_change()


async def remove_url_from_scope(url: str) -> tuple[bool, list[tuple[str, dict[str, Any]]]]:
    """Unmap ``url``, cascading its scope out of every token policy when the scope loses its last url.

    Returns ``(existed, [(user_id, committed_body), …])``; the policy cache is invalidated when the
    url existed.
    """
    existed, affected = await access_control_store().remove_url_from_scope(url)
    if existed:
        await record_policy_change()
    return existed, affected


async def remove_scope(scope_id: str) -> tuple[int, list[tuple[str, dict[str, Any]]]]:
    """Delete a scope, stripping it from every token policy and deleting its routes.

    Returns ``(deleted_count, [(user_id, committed_body), …])``; the policy cache is invalidated
    when anything was deleted. Removing the public marker or the universal grant ``"*"``
    raises ``ValueError`` with nothing written.
    """
    deleted, affected = await access_control_store().remove_scope(scope_id)
    if deleted > 0:
        await record_policy_change()
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
    await record_policy_change()


async def unpin_public_route(url: str) -> bool:
    """Unpin a public ``url``. Returns ``False`` when it was not pinned public; else the policy cache is invalidated."""
    unpinned = await access_control_store().unpin_public_route(url)
    if unpinned:
        await record_policy_change()
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
    await record_policy_change()


async def create_principal_policy(
    user_id: str, scopes: list[str], policy_data: dict[str, Any] | None, condition: dict[str, Any] | None
) -> None:
    """Insert a principal's own policy row (the backup restore's create); the policy cache is invalidated."""
    await access_control_store().create_policy(user_id, scopes, policy_data, condition)
    await record_policy_change()


async def set_principal_disabled_row(user_id: str, disabled: bool) -> None:
    """Flip a principal's ``disabled`` marker on both homes with no last-admin count; the policy cache is invalidated.

    The raw flip the backup restore applies to a recreated principal. The guarded flip every
    administrative door uses is :func:`~tai42_skeleton.access_control.roles.set_principal_disabled`.
    Raises ``KeyError`` when the principal or its policy row is absent.
    """
    await access_control_store().set_principal_disabled(user_id, disabled)
    await record_policy_change()


async def restore_policy_body(user_id: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """Write a prior policy ``body`` back as the enforced policy — the store side of a version rollback.

    Returns the restored body, or ``None`` if ``user_id`` is not provisioned (a falsy sentinel the
    route's 404 guard tests). A restored body invalidates the policy cache. A principal's own row
    is written under the last-admin guard: a body that demotes the last enabled admin principal
    raises :class:`~tai42_contract.accounts.errors.LastAdminError` with nothing written.
    """
    store = access_control_store()
    if await store.get_principal(user_id) is not None:
        async with store.principal_guard_txn() as guard:
            restored = await guard.restore_policy_body(user_id, body)
    else:
        restored = await store.restore_policy_body(user_id, body)
    if restored is not None:
        await record_policy_change()
    return restored


# -- key mint / revoke / edit (cross-backend orchestration) ------------------


async def _refuse_unmintable_policy(
    scopes: list[str], policy_data: dict[str, Any] | None, condition: TemplatedText | None
) -> None:
    """Refuse, before any write, a mint policy no key may carry; ``ValueError`` names why."""
    # The ``disabled`` claim is server-owned like the fingerprint and the owner claim,
    # but a supplied value is refused rather than stamped over: a key starts enabled and is
    # switched off by revoking it.
    refuse_disabled_claim_at_mint(policy_data or {})
    # A key with neither a scope nor a condition grants nothing, and every door reads such a
    # policy as no key at all (``policy_is_empty``), so it is refused rather than minted.
    if not scopes and condition is None:
        raise ValueError(
            "an api key needs at least one scope or a condition: a key with neither grants nothing, "
            "and every door treats it as no key"
        )
    if scopes:
        # A scope-typo guard. The universal "*" names no routed scope, so it is always
        # valid to mint; every other scope must exist in the route table.
        valid = set((await access_control_store().get_all_existing_scopes()).values())
        for scope in scopes:
            if scope != UNIVERSAL_SCOPE and scope not in valid:
                raise ValueError(f"scope {scope!r} does not exist or has no urls assigned")


async def add_user_api_key(
    user_id: str,
    description: str,
    scopes: list[str],
    policy_data: dict[str, Any] | None = None,
    condition: TemplatedText | None = None,
    *,
    owner_user_id: str,
    owner_may_be_disabled: bool = False,
) -> tuple[str, dict[str, Any], str]:
    """Provision a new key for ``user_id`` and return ``(raw_sk_key, committed_body, key_fingerprint)``.

    The tuple is the raw ``sk-…`` (surfaced to the caller exactly once), the exact policy body
    committed to Postgres (so the caller records it as durable version history without re-reading
    the store), and the fresh per-mint ``key_fingerprint`` (so the caller can surface it for a
    subsequent bind).

    Every mint stamps a fresh ``key_fingerprint`` (``uuid4`` hex) into the committed
    ``policy_data`` under :data:`KEY_FINGERPRINT_CLAIM` — the key's immutable, per-mint,
    non-reusable identity a hook binding resolves against at fire, so a revoke+remint of
    the same ``user_id`` leaves an old binding resolving to nothing.

    ``owner_user_id`` is REQUIRED: every api key belongs to a principal. The owner must be
    an EXISTING, ENABLED principal (a loud ``ValueError`` naming the state otherwise);
    ``owner_may_be_disabled`` admits a disabled owner, left disabled — the backup restore
    re-mints an archived key whose owner the archive holds disabled, and every door denies
    that key while its owner stays disabled (the standing check's owner test). The
    owner claim is DUAL-HOMED at this single mint — written into the committed
    ``policy_data`` under ``OWNER_USER_ID_CLAIM``, the owner every door reads, and persisted
    by the provider on the identity record, whose copy arrives on every request and is
    asserted equal to the stored one wherever a credential is verified. Both homes are
    written only by this mint path.

    ``policy_data`` may not carry the ``disabled`` claim: that is the enforcement projection
    of a principal's ``disabled`` state, written only by the principal's disabled writer, and
    a key is switched off by revoking it.

    ORCHESTRATES the backends in a FAIL-CLOSED order. Raises ``ValueError`` if the
    user id is already provisioned, if the owner is not an enabled principal (unless
    ``owner_may_be_disabled``), if ``policy_data`` carries the ``disabled`` claim, if the
    key has neither a scope nor a condition, or if any requested scope does not exist (has
    no url mapping) — all checked BEFORE any write, so a user error never leaves a
    half-provisioned key. Then:

    1. the provider's ``provision`` writes the identity/key record FIRST — the key
       authenticates but GRANTS NOTHING until the policy below exists;
    2. the policy row is written to the PG store, then the policy cache is invalidated.

    The per-user live-context hash ``ac:context:{user_id}`` needs NO seed — a Redis
    hash is created by its first counter write, and an absent hash reads as an empty
    live context via ``HGETALL``.

    A failure in step 2 RAISES loudly: the key exists but is denied everything, so
    the documented recovery is ``revoke_api_key(user_id)`` then a fresh mint (the
    mint is NOT idempotent — a plain retry hits the duplicate guard).
    """
    provider = _identity_provider()
    store = access_control_store()

    # The owner MUST be an existing principal, enabled unless the caller admits a disabled
    # one — every api key belongs to one. Checked with no side effect, so an
    # ownerless/disabled/unknown owner raises before the provider mints anything.
    if not owner_user_id:
        raise ValueError("owner_user_id is required: every api key belongs to a principal")
    owner_principal = await store.get_principal(owner_user_id)
    if owner_principal is None:
        raise ValueError(f"owner principal {owner_user_id!r} does not exist; a key must belong to a principal")
    if owner_principal["disabled"] and not owner_may_be_disabled:
        raise ValueError(f"owner principal {owner_user_id!r} is disabled; cannot mint a key for it")

    # Pre-checks with NO side effect, so a duplicate user or an unknown scope raises
    # before the provider mints anything (never a half-provisioned key). An orphaned
    # policy row is refused with its own recovery text — a fresh mint never silently
    # adopts a stale policy.
    state = await api_key_state(user_id)
    if state == "orphaned":
        raise ValueError(
            f"user id {user_id!r} has an orphaned policy row (its identity record is gone); "
            "re-import the access_control backup to re-mint it, or revoke it first"
        )
    if state in ("live", "account"):
        raise ValueError(
            f"user id {user_id!r} is already in use; to replace its key, revoke the key for "
            "that user id, then re-import"
        )
    await _refuse_unmintable_policy(scopes, policy_data, condition)

    # 1. Identity record FIRST (fail-closed order) — the provider owns it. The owner
    #    claim rides the record so it surfaces in AuthIdentity.claims on every request.
    raw_key = await provider.provision(user_id, description, owner_user_id=owner_user_id)

    # Fresh per-mint fingerprint: the immutable identity a hook binding resolves against
    # at fire, so a binding to a previous mint of this user_id fails closed. The owner
    # claim is always the second home the management/listing surface reads.
    key_fingerprint = uuid4().hex
    policy_data = {
        **(policy_data or {}),
        KEY_FINGERPRINT_CLAIM: key_fingerprint,
        OWNER_USER_ID_CLAIM: owner_user_id,
    }

    try:
        # 2. Policy row. The live-context hash needs no seed — it is created by the
        #    first counter write and an absent hash reads as an empty context.
        body = await store.create_policy(
            user_id, scopes, policy_data, condition.model_dump() if condition is not None else None
        )
    except Exception as exc:
        raise RuntimeError(
            f"api key for user {user_id!r} was provisioned but its policy write failed; the key "
            f"authenticates but is denied everything — recover with revoke_api_key({user_id!r}) then re-mint"
        ) from exc
    await record_policy_change()
    return raw_key, body, key_fingerprint


async def edit_user_payload(
    user_id: str,
    description: str | _Unset = _UNSET,
    scopes: list[str] | _Unset = _UNSET,
    policy_data: dict[str, Any] | _Unset | None = _UNSET,
    condition: TemplatedText | _Unset | None = _UNSET,
) -> dict[str, Any] | None:
    """Partially update an existing key's description and policy in place (never rotates the key).

    Only the arguments the caller actually supplies are written; an argument left at its ``_UNSET``
    default preserves the stored value, so editing one field never clears another. A field supplied
    as ``None``/``{}``/``""`` is written verbatim (an explicit clear).

    The edit SPLITS by field across the backends: ``description`` — whose single home
    is the identity record — goes to the provider's ``update_description``; the policy
    fields go to the PG store. The ``_UNSET`` partial-edit semantics are resolved at
    this boundary, so neither the store nor the provider ever sees ``_UNSET``.

    Returns the committed policy body on success, or ``None`` if ``user_id`` is not
    provisioned (a falsy sentinel the route's 404 guard tests). Raises ``ValueError``
    if any supplied scope does not exist, or if the edit leaves the policy with neither a
    scope nor a condition (the message names the repair for a key or for a principal). A
    principal's own row is written under the last-admin guard: an edit that demotes the
    last enabled admin principal raises
    :class:`~tai42_contract.accounts.errors.LastAdminError` with nothing written, ahead of
    the empty-policy refusal.
    """
    provider = _identity_provider()
    store = access_control_store()

    # Resolve _UNSET to the fields actually supplied; the store sees only those.
    updates: dict[str, Any] = {}
    if not isinstance(scopes, _Unset):
        updates["scopes"] = scopes
    if not isinstance(policy_data, _Unset):
        updates["policy_data"] = policy_data
    if not isinstance(condition, _Unset):
        updates["condition"] = condition.model_dump() if condition is not None else None

    if await store.get_principal(user_id) is not None:
        async with store.principal_guard_txn() as guard:
            policy = await guard.update_policy_fields(user_id, updates)
    else:
        policy = await store.update_policy_fields(user_id, updates)
    # No policy row → not provisioned. The description edit below is never attempted
    # for a user with no policy (its single existence signal this surface can read).
    if policy is None:
        return None
    # The policy write is committed: invalidate before the description step, which can raise.
    await record_policy_change()

    # Description edit → the provider (its single home). Only a supplied description
    # reaches the provider; ``update_description`` returning ``False`` for a user
    # whose policy we just wrote means the row is orphaned (its identity record is
    # gone), whose only home was that description, so raise loudly rather than silently.
    if not isinstance(description, _Unset) and not await provider.update_description(user_id, description):
        raise RuntimeError(
            f"user id {user_id!r} has an orphaned policy row (its identity record is gone) while its policy "
            "exists — cannot update the description; re-import the access_control backup to re-mint it, or revoke it"
        )
    return policy


async def remint_orphaned_api_key(user_id: str, description: str) -> str:
    """Re-mint the identity record for an orphaned key onto its surviving policy row; return the raw key.

    An orphan is a policy row minted as an api key whose identity record is gone
    (:func:`api_key_state` reads ``orphaned``) — the partial-restore state where the
    Postgres policy survived a Redis flush. This mints a FRESH identity record for the
    SAME ``user_id`` through the provider, re-homing the owner claim from the policy
    row's management home (``policy_data[OWNER_USER_ID_CLAIM]``) onto the identity
    record, whose copy every verified request is asserted against, so the restored key
    carries its original ownership.

    The policy row is NOT touched: its scopes, condition, key fingerprint and owner
    claim stay, so a hook binding keyed on the fingerprint keeps resolving after the
    restore. Returns the raw ``sk-…`` key (surfaced to the caller once).

    Raises ``ValueError`` when ``user_id`` is not in the ``orphaned`` state — never a
    silent re-mint over a live key or an account row.
    """
    state = await api_key_state(user_id)
    if state != "orphaned":
        raise ValueError(
            f"user id {user_id!r} is not an orphaned api key policy (state {state!r}); re-mint applies only "
            "to a policy row minted as an api key whose identity record is gone"
        )
    provider = _identity_provider()
    store = access_control_store()
    body = await store.get_policy_body(user_id)
    owner_user_id = ((body or {}).get("policy_data") or {}).get(OWNER_USER_ID_CLAIM)
    if not owner_user_id:
        # A minted policy row with no owner claim is an ownerless key row: it has no
        # principal to re-home onto. The clean break re-initializes, never a silent
        # ownerless re-mint.
        raise ValueError(f"user id {user_id!r} has an ownerless key row (no owner claim); re-initialize the deployment")
    raw_key = await provider.provision(user_id, description, owner_user_id=owner_user_id)
    logger.info("access_control: re-minted identity for orphaned api key policy user_id=%s", user_id)
    return raw_key


async def _has_identity_record(provider: ApiKeyIdentityProvider, user_id: str) -> bool:
    """Whether the provider still holds an identity record for ``user_id``.

    The enumeration is the provider API's only NON-destructive existence read (``revoke``
    answers by destroying the record). A provider fault propagates: a revoke must never
    proceed on a guessed existence.
    """
    return any(uid == user_id for uid, _description in await provider.list_identities())


async def _clear_policy_records(user_id: str) -> None:
    """Delete the PG policy row, bump the policy version, and delete the live-context hash.

    The FAIL-CLOSED order revoke depends on: the PG policy row FIRST (the whole authority a
    bound background execution runs on, so the fire dies even if a later step fails); the
    policy-version bump IMMEDIATELY behind it (the enforcer's cache is keyed
    ``(user_id, version)``, so a warm slot serves the deleted key's scopes until the bump
    lands); then the live-context hash ``ac:context:{user_id}`` (a residue would hand a
    future remint of the same ``user_id`` the dead key's usage/quota counters). A failure in
    any step RAISES loudly; every step is retriable.
    """
    s = _settings()
    store = access_control_store()
    await store.delete_policy(user_id)
    await bump_policy_version()
    async with client_ctx(RedisClient, s.redis) as r:
        await awaited(r.delete(_context_key(s, user_id)))


async def revoke_api_key(user_id: str) -> bool:
    """Delete a provisioned key and all of its records; clear an orphaned policy row.

    Returns ``True`` for a LIVE key (an identity record exists) and for an ORPHANED policy
    row (a row minted as an api key — it carries the key fingerprint — whose identity record
    is already gone). Returns ``False`` for an unknown id and for a role-assigned ACCOUNT
    row (no fingerprint, no identity record) this surface must never delete.

    The existence signal is the PROVIDER's identity record, read BEFORE deleting anything and
    destroyed LAST: so a half-finished revoke of a live key still has the evidence a plain
    retry needs (the policy row is deleted first, but the surviving identity record keeps the
    retry on the live path). Only when no identity record exists does the policy row's
    fingerprint decide the orphan-vs-account branch.

    The record teardown (:func:`_clear_policy_records`) runs fail-closed; for a live key the
    provider's ``revoke`` runs LAST, once every other record is gone. An orphaned row has no
    identity record, so that final step is skipped. A failure mid-teardown RAISES loudly; the
    residue (a key that verifies while holding no policy) is refused by the auth backend and a
    repeat call clears it.
    """
    provider = _identity_provider()
    store = access_control_store()

    if await _has_identity_record(provider, user_id):
        await _clear_policy_records(user_id)
        # A concurrent revoke that won the race reached the same outcome, so still report True.
        await provider.revoke(user_id)
        logger.info("access_control: revoked api key for user_id=%s", user_id)
        return True

    # No identity record: an orphaned MINTED policy row (it carries the key fingerprint) is
    # cleared here; a role-assigned account row and an unknown id are left untouched (False).
    body = await store.get_policy_body(user_id)
    if body is None or not is_minted_policy(body):
        return False
    await _clear_policy_records(user_id)
    logger.info("access_control: revoked orphaned api key policy for user_id=%s (no identity record)", user_id)
    return True


async def bump_policy_version() -> int:
    """Increment the policy-version counter (plain Redis), forcing a cross-worker cache miss on the next read.

    Returns the new version. A failed bump RAISES loudly — it is never swallowed. Inside an
    open request scope the new version replaces the remembered one, so the rest of that
    access-control decision reads what this bump wrote.
    """
    s = _settings()
    async with client_ctx(RedisClient, s.redis) as r:
        version = await awaited(r.incr(s.policy_version_key))
    remember_policy_version(version)
    return version
