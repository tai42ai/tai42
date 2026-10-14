"""Driving the generic member-actions doors from the e2e suites.

Member administration flows through one generic seam: the catalog
(``GET /api/auth/member-actions``) declares each action with an opaque key and a generic
scope; the directory (``GET /api/auth/members``) carries each row's opaque handle and the
action keys applicable to it; and the invoke door
(``POST /api/auth/member-actions/invoke``) performs an action by
``{action_key, target_handle, input}``. These helpers resolve an action by its generic
scope — and, where a scope holds more than one action, its ``destructive`` flag — never by
a provider label, the same way a generic Members screen drives the seam. Every accounts
provider declares its own actions, so a row action is resolved among the keys the target
row carries, and the invite by the one-time token its declared result carries. Row
handles are opaque tokens read back from the directory, never minted by the caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from tai42_e2e.httpapi import ApiClient

CATALOG_PATH = "/api/auth/member-actions"
INVOKE_PATH = "/api/auth/member-actions/invoke"
MEMBERS_PATH = "/api/auth/members"

_INVITE_TOKEN = "invite_token"


async def resolve_action_key(
    admin: ApiClient, *, scope: str, destructive: bool | None = None, retry_on_reloading: bool = False
) -> str:
    """The opaque catalog key of the one declared action with ``scope`` (and ``destructive``).

    Reads the catalog as the admin caller and selects by generic scope alone, or by scope
    plus the ``destructive`` flag where a scope holds more than one action. Exactly one match
    is required — zero or many is a loud failure, never a silent pick.
    """
    catalog = await admin.get(CATALOG_PATH, retry_on_reloading=retry_on_reloading)
    matches = [
        action
        for action in catalog["actions"]
        if action["scope"] == scope and (destructive is None or action["destructive"] == destructive)
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one member action for scope={scope!r} destructive={destructive!r}, "
            f"got {[a['key'] for a in matches]} from {catalog}"
        )
    return matches[0]["key"]


async def resolve_invite_action_key(admin: ApiClient, *, retry_on_reloading: bool = False) -> str:
    """The opaque catalog key of the one page action whose result carries an ``invite_token``.

    Every accounts provider may declare its own page-scoped invite; the suites accept a
    one-time invite token, so the invite is selected by that declared result field. Exactly
    one match is required — zero or many is a loud failure, never a silent pick.
    """
    catalog = await admin.get(CATALOG_PATH, retry_on_reloading=retry_on_reloading)
    matches = [
        action
        for action in catalog["actions"]
        if action["scope"] == "page" and _INVITE_TOKEN in action["result_schema"].get("properties", {})
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one page action whose result declares {_INVITE_TOKEN!r}, "
            f"got {[a['key'] for a in matches]} from {catalog}"
        )
    return matches[0]["key"]


async def resolve_row_action_key(
    admin: ApiClient, handle: str, *, scope: str, destructive: bool | None = None, retry_on_reloading: bool = False
) -> str:
    """The opaque catalog key of the one action with ``scope`` (and ``destructive``) that the
    directory row ``handle`` offers.

    Joins the row's ``action_keys`` to the catalog by key, as a Members screen renders a
    row's menu. Exactly one match is required — zero or many is a loud failure.
    """
    catalog = await admin.get(CATALOG_PATH, retry_on_reloading=retry_on_reloading)
    row = await _directory_row(admin, "handle", handle, retry_on_reloading=retry_on_reloading)
    offered = set(row["action_keys"])
    matches = [
        action
        for action in catalog["actions"]
        if action["key"] in offered
        and action["scope"] == scope
        and (destructive is None or action["destructive"] == destructive)
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one member action for scope={scope!r} destructive={destructive!r} "
            f"on row {handle!r}, got {[a['key'] for a in matches]} from {catalog}"
        )
    return matches[0]["key"]


async def members_directory(admin: ApiClient, *, retry_on_reloading: bool = False) -> dict[str, Any]:
    """The aggregated Members directory: ``{"members": [...], "invites": [...]}``."""
    return await admin.get(MEMBERS_PATH, retry_on_reloading=retry_on_reloading)


async def _directory_row(admin: ApiClient, field: str, value: str, *, retry_on_reloading: bool) -> dict[str, Any]:
    """The directory row (member or invite) whose ``field`` equals ``value``, or raise loudly."""
    directory = await members_directory(admin, retry_on_reloading=retry_on_reloading)
    for row in [*directory["members"], *directory["invites"]]:
        if row[field] == value:
            return row
    raise AssertionError(f"no member or invite row with {field} {value!r} in {directory}")


async def find_row(admin: ApiClient, email: str, *, retry_on_reloading: bool = False) -> dict[str, Any]:
    """The directory row (member or invite) whose ``email`` matches, or raise loudly.

    Returns the wire row carrying the opaque ``handle`` and the person's stable ``id``.
    """
    return await _directory_row(admin, "email", email, retry_on_reloading=retry_on_reloading)


async def handle_for_user(admin: ApiClient, user_id: str, *, retry_on_reloading: bool = False) -> str:
    """The opaque routing handle of the directory row whose stable ``id`` matches ``user_id``.

    A caller that holds a person's stable id (minted elsewhere) reads back the opaque handle
    the admin actions target, rather than minting one.
    """
    row = await _directory_row(admin, "id", user_id, retry_on_reloading=retry_on_reloading)
    return row["handle"]


async def invite_member(admin: ApiClient, *, email: str, role: str, retry_on_reloading: bool = False) -> dict[str, Any]:
    """Invite a new member through the page-scoped invite; return its identity and invite.

    Invokes the page action that returns a one-time invite token with ``{email, role}`` and reads the new row back from
    the directory. Returns ``{invite_token, login_path, user_id, handle}`` — the one-time
    link from the action result, and the person's stable id and opaque routing handle from
    the directory, the tokens later admin actions target.
    """
    key = await resolve_invite_action_key(admin, retry_on_reloading=retry_on_reloading)
    invoked = await admin.post(
        INVOKE_PATH,
        json={"action_key": key, "target_handle": None, "input": {"email": email, "role": role}},
        retry_on_reloading=retry_on_reloading,
    )
    result = invoked["result"]
    row = await find_row(admin, email, retry_on_reloading=retry_on_reloading)
    return {
        "invite_token": result["invite_token"],
        "login_path": result["login_path"],
        "user_id": row["id"],
        "handle": row["handle"],
    }


async def invoke_raw(
    api: ApiClient, *, action_key: str, target_handle: str | None = None, action_input: dict[str, Any] | None = None
) -> httpx.Response:
    """POST the invoke door and return the raw response, for a caller asserting the status."""
    return await api.request_raw(
        "POST",
        INVOKE_PATH,
        json={"action_key": action_key, "target_handle": target_handle, "input": action_input or {}},
    )


async def update_member_raw(
    admin: ApiClient, handle: str, *, role: str | None = None, disabled: bool | None = None
) -> httpx.Response:
    """Invoke the non-destructive member-row action ``handle`` offers (change role/access)."""
    key = await resolve_row_action_key(admin, handle, scope="member_row", destructive=False)
    action_input: dict[str, Any] = {}
    if role is not None:
        action_input["role"] = role
    if disabled is not None:
        action_input["disabled"] = disabled
    return await invoke_raw(admin, action_key=key, target_handle=handle, action_input=action_input)


async def update_member(
    admin: ApiClient, handle: str, *, role: str | None = None, disabled: bool | None = None
) -> None:
    """Change a member's role and/or access, asserting the invoke succeeds."""
    response = await update_member_raw(admin, handle, role=role, disabled=disabled)
    if response.status_code != 200:
        raise AssertionError(f"member update must succeed: {response.status_code} {response.text}")


async def remove_member_raw(admin: ApiClient, handle: str) -> httpx.Response:
    """Invoke the destructive member-row action ``handle`` offers (remove the member)."""
    key = await resolve_row_action_key(admin, handle, scope="member_row", destructive=True)
    return await invoke_raw(admin, action_key=key, target_handle=handle)
