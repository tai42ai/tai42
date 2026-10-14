"""Harness self-test for the member-action helpers: with several accounts providers, each
declaring its own actions, a row action resolves among the keys the directory row carries,
and the invite resolves to the one page action whose result carries a one-time token."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

from tai42_e2e import member_admin

if TYPE_CHECKING:
    from tai42_e2e.httpapi import ApiClient

# Pure harness self-test: it drives an in-memory stand-in for the member doors, never a stack.
pytestmark = [pytest.mark.backendless, pytest.mark.needs("no-stack")]


def _action(key: str, scope: str, *, destructive: bool, result: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "key": key,
        "label": key,
        "scope": scope,
        "destructive": destructive,
        "result_schema": {"type": "object", "properties": {name: {"type": "string"} for name in result}},
    }


# Two providers, each declaring its own page invite and member-row actions; the second
# declares two destructive row actions, and its invite returns a record, not a token.
_CATALOG = {
    "actions": [
        _action("a-invite", "page", destructive=False, result=("invite_token", "login_path")),
        _action("b-invite", "page", destructive=False, result=("person_id", "expires_at")),
        _action("a-update", "member_row", destructive=False),
        _action("a-remove", "member_row", destructive=True),
        _action("b-update", "member_row", destructive=False),
        _action("b-remove-login", "member_row", destructive=True),
        _action("b-remove", "member_row", destructive=True),
    ]
}
_DIRECTORY = {
    "members": [
        {"id": "p-a", "email": "a@example.test", "handle": "h-a", "action_keys": ["a-update", "a-remove"]},
        {
            "id": "p-b",
            "email": "b@example.test",
            "handle": "h-b",
            "action_keys": ["b-update", "b-remove-login", "b-remove"],
        },
    ],
    "invites": [
        {"id": "p-c", "email": "c@example.test", "handle": "h-c", "action_keys": []},
    ],
}


class _StubApi:
    """The catalog and directory doors, and a record of every invoke."""

    def __init__(self) -> None:
        self.invoked: list[dict[str, Any]] = []

    async def get(self, path: str, *, retry_on_reloading: bool = False) -> dict[str, Any]:
        return {member_admin.CATALOG_PATH: _CATALOG, member_admin.MEMBERS_PATH: _DIRECTORY}[path]

    async def post(self, path: str, *, json: dict[str, Any], retry_on_reloading: bool = False) -> dict[str, Any]:
        assert path == member_admin.INVOKE_PATH
        self.invoked.append(json)
        return {"result": {"invite_token": "tok", "login_path": "/login?invite=tok"}}

    async def request_raw(self, method: str, path: str, *, json: dict[str, Any]) -> httpx.Response:
        assert (method, path) == ("POST", member_admin.INVOKE_PATH)
        self.invoked.append(json)
        return httpx.Response(200, json={"data": {"result": {}}})


def _api(stub: _StubApi) -> ApiClient:
    return cast("ApiClient", stub)


async def test_row_actions_resolve_among_the_rows_own_keys() -> None:
    stub = _StubApi()
    await member_admin.remove_member_raw(_api(stub), "h-a")
    await member_admin.update_member_raw(_api(stub), "h-a", role="viewer")
    assert [(call["action_key"], call["target_handle"]) for call in stub.invoked] == [
        ("a-remove", "h-a"),
        ("a-update", "h-a"),
    ]


async def test_a_row_with_two_matching_actions_fails_loudly() -> None:
    with pytest.raises(AssertionError, match="exactly one member action"):
        await member_admin.remove_member_raw(_api(_StubApi()), "h-b")


async def test_an_unknown_handle_fails_loudly() -> None:
    with pytest.raises(AssertionError, match="no member or invite row"):
        await member_admin.remove_member_raw(_api(_StubApi()), "h-missing")


async def test_the_invite_resolves_to_the_page_action_returning_a_token() -> None:
    stub = _StubApi()
    invited = await member_admin.invite_member(_api(stub), email="c@example.test", role="editor")
    assert stub.invoked == [
        {"action_key": "a-invite", "target_handle": None, "input": {"email": "c@example.test", "role": "editor"}}
    ]
    assert invited == {"invite_token": "tok", "login_path": "/login?invite=tok", "user_id": "p-c", "handle": "h-c"}
