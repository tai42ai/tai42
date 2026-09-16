"""Re-mint the owner's key when a deployment is initialized but no owner key can authenticate.

The partial-restore state: the Postgres policy store survived a flush of the access-control
Redis, so the owner principal keeps the ``POST /api/setup`` door shut (409, a principal
exists) while every one of the owner's api-key policy rows is an ORPHAN — its identity
record is gone, so it authenticates nothing. With no backup export there is no re-import to
re-mint the orphans, so this is the way back in.

It runs behind the SAME gate the setup door uses — the per-source throttle, the constant-time
token compare, and the mint mutex — under a ``local`` source bucket, because its only caller
is the host-side command that holds the deployment's env. It re-mints a fresh identity record
onto ONE surviving owner policy row through :func:`~tai42_skeleton.access_control.management.remint_orphaned_api_key`
(the policy row, fingerprint and owner claim stay), returns the plaintext once, and creates or
touches nothing else. It refuses loudly — every refusal is a :class:`SetupRecoveryError` — when
the owner already holds a live key, when no owner key policy survives, or when the owner marker
(the unique principal with ``created_by IS NULL``) is missing or ambiguous.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM

from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.access_control.setup_gate import (
    SetupContendedError,
    clear_setup_failures,
    record_setup_failure,
    setup_lock,
    setup_throttle_locked,
    verify_setup_token,
)
from tai42_skeleton.access_control.store import access_control_store

logger = logging.getLogger(__name__)

# The host-side door's bucket under the SAME throttle/failure keys the HTTP setup door uses.
LOCAL_SOURCE = "local"


class SetupRecoveryError(RuntimeError):
    """A recovery refusal; its message is the operator-facing text the command renders."""


async def recover_owner_key(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict[str, Any]:
    """Re-mint one surviving owner key behind the setup gate; return the plaintext once.

    The gate mirrors the setup door's own order and reuses its functions: refuse when access
    control is off; consult the ``local`` throttle BEFORE comparing (a wrong-token flood
    escalates a backoff that turns further attempts away without comparing); a wrong/absent
    token is a generic ``Forbidden`` (no oracle, ``TAI_SETUP_OPEN`` opens it as for the door);
    refuse when no provider can mint. Under the shared mint mutex — so recovery and an
    initialize never interleave — the owner principal, the owner's surviving key rows, the
    live-key guard and the row choice are resolved, then exactly ONE row is re-minted. On
    success the ``local`` failure counter is cleared and the action is logged loudly.
    """
    if not access_control_settings().enable:
        raise SetupRecoveryError("access control is off (ACCESS_CONTROL_ENABLE); there is no key to recover")

    if await setup_throttle_locked(LOCAL_SOURCE):
        logger.warning("setup recovery: throttled")
        raise SetupRecoveryError("Forbidden")

    if not await verify_setup_token(setup_token):
        await record_setup_failure(LOCAL_SOURCE)
        logger.warning("setup recovery: token mismatch/absent")
        raise SetupRecoveryError("Forbidden")

    if not any(mintable for _name, mintable in management.provider_capabilities()):
        raise SetupRecoveryError("no configured identity provider can mint api keys; nothing can be re-minted")

    try:
        async with setup_lock():
            owner = await _owner_principal()
            owner_id = owner["user_id"]
            candidates = await _owner_key_candidates(owner_id)
            await _refuse_if_owner_has_live_key(candidates)
            if not candidates:
                raise SetupRecoveryError(
                    "the owner has no surviving key policy row to re-mint; restore the access_control backup, "
                    "or re-initialize on a fresh baseline with `tai setup`"
                )
            key_id, body = _choose_key(candidates, key_user_id)
            try:
                raw_key = await management.remint_orphaned_api_key(key_id, key_description)
            except ValueError as exc:
                raise SetupRecoveryError(str(exc)) from exc
    except SetupContendedError as exc:
        raise SetupRecoveryError("a concurrent setup holds the mint lock; retry") from exc

    await clear_setup_failures(LOCAL_SOURCE)
    logger.warning(
        "setup recovery: re-minted the owner's key user_id=%s for owner=%s "
        "(new identity record; policy row, fingerprint and owner claim unchanged)",
        key_id,
        owner_id,
    )
    return {
        "owner_user_id": owner_id,
        "key_user_id": key_id,
        "api_key": raw_key,
        "key_fingerprint": body["policy_data"][KEY_FINGERPRINT_CLAIM],
    }


async def _owner_principal() -> dict[str, Any]:
    """The unique owner principal (the one with ``created_by IS NULL``), or a loud refusal.

    The setup door is the only writer of a NULL-creator principal, so the owner is that one
    row. No NULL-creator row while principals exist, or more than one, is an invariant breach
    named rather than guessed; no principals at all means the deployment is not initialized.
    """
    owners = [p for p in await management.list_principals() if p.get("created_by") is None]
    if not owners:
        if not await management.any_principal_exists():
            raise SetupRecoveryError("this deployment is not initialized; run `tai setup`")
        raise SetupRecoveryError(
            "no owner principal (created_by NULL) while principals exist: the owner marker is missing; "
            "restore the access_control backup or re-initialize the deployment"
        )
    if len(owners) > 1:
        ids = ", ".join(sorted(p["user_id"] for p in owners))
        raise SetupRecoveryError(
            f"several owner principals (created_by NULL): {ids}; the owner marker must be unique — "
            "restore the access_control backup"
        )
    return owners[0]


async def _owner_key_candidates(owner_id: str) -> list[tuple[str, dict[str, Any]]]:
    """The owner's minted key policy rows as ``(user_id, body)`` — every other principal's rows excluded.

    Reads the minted policy rows (each carries the key fingerprint) and keeps only those whose
    ``policy_data[OWNER_USER_ID_CLAIM]`` is the owner. This is the ONLY read of any key row and
    it never mutates one; another principal's orphaned key is never re-minted here.
    """
    return [
        (user_id, body)
        for user_id, body in await access_control_store().list_minted_policies()
        if (body.get("policy_data") or {}).get(OWNER_USER_ID_CLAIM) == owner_id
    ]


async def _refuse_if_owner_has_live_key(candidates: list[tuple[str, dict[str, Any]]]) -> None:
    """Refuse when any of the owner's keys still authenticates — recovery is for the no-live-key state only."""
    for user_id, _body in candidates:
        if await management.api_key_state(user_id) == "live":
            raise SetupRecoveryError(
                f"the owner already holds a live key ({user_id}); recovery applies only when no owner key can "
                "authenticate — use that key, or revoke it first with `tai keys delete`"
            )


def _choose_key(candidates: list[tuple[str, dict[str, Any]]], key_user_id: str | None) -> tuple[str, dict[str, Any]]:
    """The one owner key row to re-mint: the named one, the sole one, or a loud refusal to name it.

    An explicit ``key_user_id`` must be one of the owner's keys. Absent, a single candidate
    selects itself; several force the operator to name one, so exactly one plaintext is ever
    printed and the rest are re-minted through ``tai keys`` once back in.
    """
    by_id = dict(candidates)
    ids = ", ".join(sorted(by_id))
    if key_user_id is not None:
        if key_user_id not in by_id:
            raise SetupRecoveryError(f"--key-user {key_user_id!r} is not one of the owner's keys ({ids})")
        return key_user_id, by_id[key_user_id]
    if len(candidates) == 1:
        return candidates[0]
    raise SetupRecoveryError(f"the owner holds several keys ({ids}); name the one to re-mint with --key-user")
