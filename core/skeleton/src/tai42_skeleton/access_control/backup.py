"""The ``access_control`` backup section: principals, route mappings, patterns and api-key tokens.

The export carries every route mapping (public ones included), the dynamic-route
patterns, every principal with its own policy row, and every provisioned key merged with
its policy. The import restores principals first (so a token's owner exists), then the
route mappings, then the tokens, all through :mod:`~tai42_skeleton.access_control.management`.
The whole import runs inside :func:`~tai42_skeleton.access_control.management.policy_write_batch`,
so the policy cache is invalidated once at the end — and still when a write part-way raises.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, Final

from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.backup import BackupSectionReport
from tai42_contract.template import TemplatedText

from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.backup.registry import BackupMode, _empty_report

# The policy a principal with no policy row exports, and the body a restored principal
# gets when its archive entry carries none. Copied with ``dict(...)`` at each use.
DEFAULT_POLICY_BODY: Final[Mapping[str, Any]] = MappingProxyType({"scopes": [], "policy_data": {}, "condition": None})


def _default_policy_body() -> dict[str, Any]:
    """A fresh mutable copy of :data:`DEFAULT_POLICY_BODY` (its list and dict copied too)."""
    body = dict(DEFAULT_POLICY_BODY)
    body["scopes"] = list(body["scopes"])
    body["policy_data"] = dict(body["policy_data"])
    return body


async def export_access_control() -> dict[str, Any]:
    """The section's export: ``scopes``, ``patterns``, ``principals`` and ``tokens``."""
    # Principals travel WITH their own policy row (the fingerprint-less role policy), so a
    # restore recreates the identity roster before re-minting the keys that belong to it.
    principals: list[dict[str, Any]] = []
    for principal in await management.list_principals():
        policy = await management.get_policy_body(principal["user_id"])
        created_at = principal.get("created_at")
        principals.append(
            {
                "user_id": principal["user_id"],
                "kind": principal["kind"],
                "display_name": principal["display_name"],
                "created_by": principal["created_by"],
                "disabled": principal["disabled"],
                "created_at": created_at.isoformat() if created_at is not None else None,
                "policy": policy or _default_policy_body(),
            }
        )

    return {
        # EVERY route mapping url -> value, public routes included (value
        # ``public_resource_id``), not the non-public-only ``get_all_existing_scopes``:
        # a public route must not restore as protected.
        "scopes": await management.get_all_route_mappings(),
        "patterns": await management.get_all_existing_patterns(),
        "principals": principals,
        "tokens": await management.get_all_existing_tokens_payload(),
    }


async def _restore_principals(payload: dict[str, Any], report: BackupSectionReport) -> dict[str, bool]:
    """Restore principals FIRST so the token restore below finds every owner provisioned.

    Each principal's own policy row (the fingerprint-less role policy) travels with it. An
    existing principal is identity and is never overwritten; a missing/empty id or a store
    failure is a loud per-principal error.

    A principal whose policy row already exists in Postgres (but whose principal row does
    not — a Redis-only loss recovered from a surviving Postgres) keeps the live policy: the
    policy rows in Postgres are the source of truth, matching the token restore, so the
    principal row is created and the exported policy body is not re-written.

    Returns each created principal's archived ``disabled`` flag, which
    :func:`_apply_archived_disabled` applies once the tokens are restored: a key outlives its
    owner being disabled, so an archive can hold a disabled owner's key, and a key is minted
    only for an enabled owner.
    """
    archived_disabled: dict[str, bool] = {}
    for principal in payload.get("principals") or []:
        user_id = principal.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            report.errors.append(f"principal with missing or empty user_id: {user_id!r}")
            report.skipped += 1
            continue
        if await management.get_principal(user_id) is not None:
            report.details["skipped_existing"] += 1
            continue
        try:
            await management.create_principal_row(
                user_id, principal["kind"], principal["display_name"], principal.get("created_by")
            )
            if await management.get_policy_body(user_id) is None:
                policy = principal.get("policy") or _default_policy_body()
                await management.create_principal_policy(
                    user_id, list(policy.get("scopes") or []), policy.get("policy_data"), policy.get("condition")
                )
        except (ValueError, KeyError) as exc:
            report.errors.append(f"principal {user_id!r}: {exc}")
            report.skipped += 1
            continue
        archived_disabled[user_id] = bool(principal.get("disabled"))
        report.created += 1
    return archived_disabled


async def _apply_archived_disabled(archived_disabled: dict[str, bool], report: BackupSectionReport) -> None:
    """Apply each restored principal's archived ``disabled`` flag with the raw flip (no last-admin count).

    Enabled or disabled, the archived flag is the authority: the flip writes the policy's
    ``disabled`` projection from it whatever the archived or surviving policy carried. A
    store failure is a loud per-principal error.
    """
    for user_id, disabled in archived_disabled.items():
        try:
            await management.set_principal_disabled_row(user_id, disabled)
        except KeyError as exc:
            report.errors.append(f"principal {user_id!r}: {exc}")


def _canonical_archive_urls(urls: Iterable[str]) -> dict[str, str]:
    """Each archive route url mapped to its canonical form, refusing two urls with one canonical form.

    The route table holds canonical urls, so two archive rows that reduce to one url would
    overwrite each other — the archive is refused before the first write rather than restored
    with a row silently dropped. A url with no canonical form raises its ``MalformedPathError``.
    """
    from tai42_skeleton.access_control.path_canon import canonicalize_path

    canonical: dict[str, str] = {}
    seen: dict[str, str] = {}
    for url in urls:
        form = canonicalize_path(url)
        if form in seen:
            raise ValueError(
                f"access_control archive maps {seen[form]!r} and {url!r} to one canonical route {form!r}; "
                "refusing to restore"
            )
        seen[form] = url
        canonical[url] = form
    return canonical


async def import_access_control(payload: dict[str, Any], mode: BackupMode) -> BackupSectionReport:
    """Restore the section's export under ``mode`` and return its report.

    ``skip`` leaves an already-mapped route url as it stands; ``overwrite`` re-maps it.
    Principals and tokens are create-only under both modes.
    """
    report = _empty_report()
    report.details["new_api_keys"] = []
    patterns = payload.get("patterns") or {}
    scopes = payload.get("scopes") or {}
    canonical = _canonical_archive_urls(scopes)

    # Every write below records its change; the policy cache is invalidated once at the end,
    # and still when a write part-way raises.
    async with management.policy_write_batch():
        archived_disabled = await _restore_principals(payload, report)

        # Replay route -> scope mappings first so the token restore below finds every
        # referenced scope already provisioned. Keyed by the canonical url the table holds:
        # under ``skip`` an already-mapped url is left as it stands.
        marker = access_control_settings().public_resource_id
        # Read live mappings only when there are scopes to place; a token-only restore needs no store hit.
        existing_urls = set((await management.get_all_route_mappings()).keys()) if scopes else set()
        for url, scope_id in scopes.items():
            existed = canonical[url] in existing_urls
            if existed and mode == "skip":
                report.details["skipped_existing"] += 1
                continue
            if scope_id == marker:
                # The marker is a column value, not a scope: a public route restores through
                # the dedicated pin writer, never ``add_url_to_scope``.
                await management.pin_route_public(url, patterns.get(url))
            else:
                try:
                    await management.add_url_to_scope(scope_id, url, patterns.get(url))
                except ValueError as exc:
                    # A mapping the writer refuses (a route public by its own declaration,
                    # the universal grant) is a loud per-url error; the rest restores.
                    report.errors.append(f"route {url!r}: {exc}")
                    report.skipped += 1
                    continue
            if existed:
                report.updated += 1
            else:
                report.created += 1

        await _restore_tokens(payload, report)
        await _apply_archived_disabled(archived_disabled, report)
    return report


async def _restore_token(token: dict[str, Any], report: BackupSectionReport) -> None:
    """Restore one exported api-key token, minting a fresh key and recording its plaintext.

    A user id with a live key or an account row is a clean ``skipped_existing``; an orphaned
    policy row (its identity record gone) is re-minted onto the surviving policy; a user id
    with no policy row is minted fresh with the owner threaded from the token's management
    home. A token with no owner claim is an ownerless key row — a loud per-token error,
    never re-minted ownerless.
    """
    user_id = token.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        report.errors.append(f"token with missing or empty user_id: {user_id!r}")
        report.skipped += 1
        return
    description = token.get("description", "")
    state = await management.api_key_state(user_id)
    if state in ("live", "account"):
        report.details["skipped_existing"] += 1
        return
    owner_user_id = (token.get("policy_data") or {}).get(OWNER_USER_ID_CLAIM)
    if state != "orphaned" and not (isinstance(owner_user_id, str) and owner_user_id):
        report.errors.append(f"token {user_id!r}: no owner claim (ownerless key; re-initialize the deployment)")
        report.skipped += 1
        return
    try:
        if state == "orphaned":
            api_key = await management.remint_orphaned_api_key(user_id, description)
        else:
            assert isinstance(owner_user_id, str)  # noqa: S101 — narrowed by the guard above
            stored_condition = token.get("condition")
            # Parse the stored templated-text document back through the contract so a
            # malformed one raises here, never restores as a silently dropped condition.
            condition = TemplatedText.model_validate(stored_condition) if stored_condition is not None else None
            api_key, _committed_body, _fingerprint = await management.add_user_api_key(
                user_id,
                description,
                token.get("scopes") or [],
                token.get("policy_data"),
                condition,
                owner_user_id=owner_user_id,
            )
    except ValueError as exc:
        # Per-token failure (collided id, absent scope, bad condition) surfaced loudly.
        report.errors.append(f"token {user_id!r}: {exc}")
        report.skipped += 1
        return
    report.created += 1
    report.details["new_api_keys"].append({"user_id": user_id, "description": description, "api_key": api_key})


async def _restore_tokens(payload: dict[str, Any], report: BackupSectionReport) -> None:
    """Restore every exported api-key token (see :func:`_restore_token`).

    API-key hashes are one-way, so a restore mints BRAND-NEW keys and surfaces each
    plaintext in ``new_api_keys``.
    """
    for token in payload.get("tokens") or []:
        await _restore_token(token, report)
