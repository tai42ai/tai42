"""The api-key lifecycle: mint, edit, re-mint and revoke, orchestrated across the backends.

A key spans three homes, and every op here keeps them in step:

- The api-key IDENTITY record (key hash → ``{user_id, description}`` plus its
  ``user_id`` → hash reverse lookup) is OWNED by the active identity provider plugin,
  reached ONLY through the ``ApiKeyIdentityProvider`` API (``provision``/``revoke``/
  ``update_description``/``list_identities``) resolved by
  :mod:`~tai42_skeleton.access_control.management` — this module never imports the
  plugin nor touches ``ac:key:*``.
- The key's POLICY row (scopes, condition, ``policy_data`` with the key fingerprint and
  the owner claim) lives in the Postgres
  :class:`~tai42_skeleton.access_control.store.PostgresAccessControlStore`; every
  committed policy change bumps the ``ac:policy_version`` cache-buster through
  :mod:`~tai42_skeleton.access_control.policy_version`.
- The ``ac:context:{user_id}`` per-user LIVE COUNTERS are a plain Redis HASH on the AC
  Redis, created by the first counter write and deleted at revoke here.

Backend errors are never swallowed — they propagate so a failed lifecycle op is loud.
The orchestration is fail-closed (identity record first on mint, policy row first on
revoke); a failure mid-way raises loudly rather than leaving a silent orphan, and every
step is ordered so a plain retry finishes the job.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any
from uuid import uuid4

from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM, UNIVERSAL_SCOPE
from tai42_contract.template import TemplatedText
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control import policy_version
from tai42_skeleton.access_control.management import (
    _has_identity_record,
    _identity_provider,
    api_key_state,
    is_minted_policy,
)
from tai42_skeleton.access_control.policy_rows import refuse_disabled_claim_at_mint
from tai42_skeleton.access_control.settings import AccessControlSettings, access_control_settings
from tai42_skeleton.access_control.store import access_control_store
from tai42_skeleton.template.references import assert_templates_stored
from tai42_skeleton.template.stored_refs import condition_template_refs
from tai42_skeleton.utils.redis_typing import awaited

logger = logging.getLogger(__name__)


class _Unset(Enum):
    """Sentinel marking an ``edit_user_payload`` argument the caller did not supply.

    A partial edit leaves such a field at its stored value; only fields given an explicit value
    (including ``None``/``{}``/``""``) are overwritten, so editing one field never silently
    clears another.
    """

    UNSET = "unset"


_UNSET = _Unset.UNSET


def _context_key(s: AccessControlSettings, user_id: str) -> str:
    return f"{s.context_prefix}{user_id}"


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
    key has neither a scope nor a condition, if any requested scope does not exist (has
    no url mapping), or if the condition names a stored template id the store lacks — all
    checked BEFORE any write, so a user error never leaves a half-provisioned key. Then:

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
    await assert_templates_stored(condition_template_refs(condition), ValueError)
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
    await policy_version.record_policy_change()
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
    if a supplied condition names a stored template id the store lacks (checked before any
    write; a stored condition the edit does not supply is not read), if any supplied scope
    does not exist, or if the edit leaves the policy with neither a scope nor a condition
    (the message names the repair for a key or for a principal). A
    principal's own row is written under the last-admin guard: an edit that demotes the
    last enabled admin principal raises
    :class:`~tai42_contract.accounts.errors.LastAdminError` with nothing written, ahead of
    the empty-policy refusal.
    """
    if isinstance(condition, TemplatedText):
        await assert_templates_stored(condition_template_refs(condition), ValueError)
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
    await policy_version.record_policy_change()

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
    (:func:`~tai42_skeleton.access_control.management.api_key_state` reads ``orphaned``) —
    the partial-restore state where the Postgres policy survived a Redis flush. This mints
    a FRESH identity record for the SAME ``user_id`` through the provider, re-homing the
    owner claim from the policy row's management home (``policy_data[OWNER_USER_ID_CLAIM]``) onto the identity
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
    s = access_control_settings()
    store = access_control_store()
    await store.delete_policy(user_id)
    await policy_version.bump_policy_version()
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
