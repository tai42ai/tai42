"""The access-control policy ROW: its columns, its body shape and the cursor-level writers over it.

The policy store (:mod:`tai42_skeleton.access_control.store`) and its principals half
(:mod:`tai42_skeleton.access_control.principals_mixin`) both read and write policy rows
through these functions on the caller's cursor, so the principals half never imports the
store it is mixed into.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tai42_contract.access_control import DISABLED_CLAIM, KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM
from tai42_kit.clients.impl.postgres import Json

if TYPE_CHECKING:
    from tai42_skeleton.access_control.store import PostgresAccessControlStore

# The policy body shape enforcement reads and the management surface writes.
POLICY_COLUMNS = "scopes, policy_data, condition"


# The server-owned ``policy_data`` anchors a client may never write or strip: the per-mint
# key fingerprint (a binding's identity), the owner claim (a key belongs to a principal for
# its whole life) and the ``disabled`` projection (written only by the principal's disabled
# writer). An edit or rollback drops any incoming value and carries the stored one forward,
# and an edit whose ``disabled`` value is not the stored state is refused
# (:func:`_refuse_disabled_change`); the key mint stamps the fingerprint and the owner itself
# and refuses a supplied ``disabled`` (:func:`refuse_disabled_claim_at_mint`). A row that was
# never minted carries no fingerprint or owner, and an enabled principal's row carries no
# ``disabled`` claim.
_SERVER_OWNED_POLICY_CLAIMS = (KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM, DISABLED_CLAIM)

# The refusal of a policy edit whose ``disabled`` claim differs from the stored state.
DISABLED_NOT_POLICY_CONTENT_MESSAGE = (
    "'disabled' is not policy content: a principal is disabled or re-enabled through the principals door "
    "(or its accounts provider's member action) and an API key is revoked; nothing was written"
)


def _refuse_disabled_change(new_policy_data: dict[str, Any], stored_policy_data: dict[str, Any]) -> None:
    """Raise ``ValueError`` when an edit's ``disabled`` claim is not exactly the stored state.

    The stored state is the boolean ``stored_policy_data.get("disabled") is True``. An absent
    claim, or the boolean equal to that state (a client round-tripping the body it read), is
    accepted; the stored value is then carried by :func:`_carry_server_owned_claims`. Any other
    value — the opposite boolean or a non-boolean such as ``"true"`` or ``1`` — is refused.
    """
    if DISABLED_CLAIM not in new_policy_data:
        return
    value = new_policy_data[DISABLED_CLAIM]
    if not isinstance(value, bool) or value != (stored_policy_data.get(DISABLED_CLAIM) is True):
        raise ValueError(DISABLED_NOT_POLICY_CONTENT_MESSAGE)


def refuse_disabled_claim_at_mint(policy_data: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` when a key mint's ``policy_data`` carries a ``disabled`` claim.

    A minted key starts with no ``disabled`` state and only the principal's disabled writer
    projects one, so the mint refuses any supplied value, as an edit refuses one that is not
    the stored state.
    """
    if DISABLED_CLAIM in policy_data:
        raise ValueError(DISABLED_NOT_POLICY_CONTENT_MESSAGE)


def _carry_server_owned_claims(new_policy_data: dict[str, Any], stored_policy_data: dict[str, Any]) -> dict[str, Any]:
    """Drop any client-supplied server-owned claim and carry the stored value forward."""
    for claim in _SERVER_OWNED_POLICY_CLAIMS:
        new_policy_data.pop(claim, None)
        stored = stored_policy_data.get(claim)
        if stored is not None:
            new_policy_data[claim] = stored
    return new_policy_data


def policy_body_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Assemble the canonical policy body from a ``POLICY_COLUMNS`` row.

    ``condition`` is the stored templated-text document (``{content|id, kwargs}``) or
    ``None`` when no condition is configured.
    """
    scopes, policy_data, condition = row
    return {
        "scopes": list(scopes or []),
        "policy_data": policy_data or {},
        "condition": condition,
    }


async def write_policy_body(cur: Any, user_id: str, body: dict[str, Any]) -> None:
    """Overwrite ``user_id``'s policy row with ``body`` on the caller's cursor (no row, no write)."""
    await cur.execute(
        "UPDATE access_control_policies SET scopes = %s, policy_data = %s, condition = %s WHERE user_id = %s",
        (
            body["scopes"],
            Json(body["policy_data"]),
            Json(body["condition"]),
            user_id,
        ),
    )


async def resolve_policy_update(
    store: PostgresAccessControlStore, cur: Any, user_id: str, updates: dict[str, Any]
) -> dict[str, Any] | None:
    """The body a partial policy edit commits, read and locked on ``cur``; ``None`` when no row exists.

    Supplied scopes are validated with their route rows locked ``FOR SHARE``; the row is
    locked ``FOR UPDATE``. Server-owned claims are carried from the stored row; a supplied
    ``disabled`` claim that differs from the stored state raises ``ValueError``. The caller
    refuses a body left with neither a scope nor a condition (:func:`refuse_empty_policy_edit`)
    before it writes. Writes nothing.
    """
    if updates.get("scopes"):
        await store._lock_and_validate_scopes(cur, updates["scopes"])
    await cur.execute(
        f"SELECT {POLICY_COLUMNS} FROM access_control_policies WHERE user_id = %s FOR UPDATE",  # noqa: S608 query built from constant, code-defined identifiers, not user input
        (user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    body = policy_body_from_row(row)
    if "scopes" in updates:
        body["scopes"] = updates["scopes"]
    if "policy_data" in updates:
        # An edit rewrites policy_data wholesale, but the server-owned anchors (the per-mint
        # fingerprint, the owner claim and the disabled projection) are carried from the
        # stored row: a client can never write or strip them, a hook bound to an edited (not
        # reminted) key keeps resolving, and the principal's enabled state never changes.
        new_policy_data = dict(updates["policy_data"] or {})
        _refuse_disabled_change(new_policy_data, body["policy_data"])
        body["policy_data"] = _carry_server_owned_claims(new_policy_data, body["policy_data"])
    if "condition" in updates:
        body["condition"] = updates["condition"]
    return body


# The refusal of an edit that leaves a policy with neither a scope nor a condition, per row:
# a key is revoked through the key door, a principal is disabled or deleted through the
# principals door (the key door's revoke answers 404 for a principal).
EMPTY_KEY_POLICY_EDIT_MESSAGE = (
    "the edit leaves the policy with neither a scope nor a condition: a policy with neither "
    "grants nothing, and every door treats it as no key; revoke the key instead"
)
EMPTY_PRINCIPAL_POLICY_EDIT_MESSAGE = (
    "the edit leaves the principal's policy with neither a scope nor a condition: a policy with neither "
    "grants nothing, and every door treats it as no key; disable or delete the principal instead"
)


def refuse_empty_policy_edit(updates: dict[str, Any], body: dict[str, Any], *, principal: bool) -> None:
    """Raise ``ValueError`` when an edit leaves ``body`` with neither a scope nor a condition.

    Such a policy grants nothing and reads as no key at every door (``policy_is_empty``). The
    message names the repair for the row edited: ``principal`` for a principal's own row,
    otherwise a key's. An edit that supplies no field changes nothing and is not refused.
    """
    if updates and not body["scopes"] and body["condition"] is None:
        raise ValueError(EMPTY_PRINCIPAL_POLICY_EDIT_MESSAGE if principal else EMPTY_KEY_POLICY_EDIT_MESSAGE)


async def resolve_policy_restore(cur: Any, user_id: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """The body a rollback commits, with the live server-owned claims forced on; ``None`` when no row exists.

    The row is locked ``FOR UPDATE`` on ``cur``. Writes nothing.
    """
    resolved = {
        "scopes": list(body.get("scopes") or []),
        "policy_data": dict(body.get("policy_data") or {}),
        "condition": body.get("condition"),
    }
    await cur.execute(
        f"SELECT {POLICY_COLUMNS} FROM access_control_policies WHERE user_id = %s FOR UPDATE",  # noqa: S608 query built from constant, code-defined identifiers, not user input
        (user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    _carry_server_owned_claims(resolved["policy_data"], policy_body_from_row(row)["policy_data"])
    return resolved
