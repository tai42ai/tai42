"""The member-actions seam, proven by a NEUTRAL accounts provider.

A test-only accounts provider — owned by neither shipped implementation — declares one
page action and one row action, implements ``invoke_member_action`` and a ``list_members``
whose rows carry its own action ids and the platform principal ids a person holds. The two
generic operations list the declared actions and invoke one by key with no platform change:
the platform mints opaque routing tokens, renders the labels and the input/result schemas
generically, validates the per-action input the ORDINARY way, and reads nothing inside a
provider result. This neutral consumer IS the proof the seam is usable by any other plugin
as it is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import BaseModel
from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.access_control.models import AccessPolicy
from tai42_contract.accounts import (
    AccountsProvider,
    InviteEntry,
    LoginMethod,
    MemberAction,
    MemberEntry,
    MemberListing,
)
from tai42_contract.accounts.errors import (
    MemberActionBadRequestError,
    MemberActionConflictError,
    MemberActionError,
    MemberActionNotFoundError,
)
from tai42_contract.template import TemplatedText

from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    OperationError,
    ValidationRejectedError,
)
from tai42_skeleton.operations import member_actions as member_actions_ops
from tai42_skeleton.operations import members as members_ops
from tai42_skeleton.operations._authority import Caller
from tai42_skeleton.operations.member_actions import (
    _decode_action_key,
    _decode_handle,
    _encode_action_key,
    _encode_handle,
)


class _PingInput(BaseModel):
    """A page action's declared input — one field, pydantic's default extra behaviour."""

    note: str


class _PingResult(BaseModel):
    echo: str


class _PokeInput(BaseModel):
    reason: str


class _PokeResult(BaseModel):
    link: str


class _NeutralActionsProvider(AccountsProvider):
    """An accounts provider owned by neither shipped implementation.

    Declares a page action (``ping``) and a member-row action (``poke``) in its OWN
    vocabulary, labelled by template id, and routes an invoke back to itself through the
    seam.
    """

    def __init__(self, listing: MemberListing, *, invoke_error: Exception | None = None) -> None:
        self._listing = listing
        self._invoke_error = invoke_error
        self.invoked: list[tuple[str, str | None, BaseModel]] = []

    async def validate_token(self, token: str) -> AuthIdentity | None:  # pragma: no cover - unused
        return None

    def login_methods(self) -> list[LoginMethod]:  # pragma: no cover - unused
        return []

    async def list_members(self) -> MemberListing:
        return self._listing

    def member_actions(self) -> list[MemberAction]:
        return [
            MemberAction(
                id="ping",
                label=TemplatedText(content="Ping the member"),
                scope="page",
                input_model=_PingInput,
                result_model=_PingResult,
            ),
            MemberAction(
                id="poke",
                label=TemplatedText(content="Poke member {{ 1 + 1 }}"),
                scope="member_row",
                destructive=True,
                input_model=_PokeInput,
                result_model=_PokeResult,
            ),
        ]

    async def invoke_member_action(self, action_id: str, *, target: str | None, payload: BaseModel) -> BaseModel:
        self.invoked.append((action_id, target, payload))
        if self._invoke_error is not None:
            raise self._invoke_error
        if action_id == "ping":
            assert isinstance(payload, _PingInput)
            return _PingResult(echo=payload.note)
        assert isinstance(payload, _PokeInput)
        return _PokeResult(link=f"link:{target}:{payload.reason}")

    async def revoke_session(self, token: str) -> bool:  # pragma: no cover - unused
        return False


def _member(id_: str, *, principal_ids: list[str] | None = None, actions: list[str] | None = None) -> MemberEntry:
    return MemberEntry(
        id=id_,
        email=f"{id_}@x.test",
        role="editor",
        created_at=datetime.now(UTC),
        principal_ids=principal_ids or [id_],
        actions=actions if actions is not None else ["poke"],
    )


def _invite(id_: str) -> InviteEntry:
    now = datetime.now(UTC)
    return InviteEntry(id=id_, email=f"{id_}@x.test", role="viewer", created_at=now, expires_at=now, actions=[])


@pytest.fixture(autouse=True)
def _clean_active_providers():
    from tai42_skeleton.app.instance import app

    core = app._serving_core
    saved = dict(core.active_auth_providers)
    core.active_auth_providers.clear()
    try:
        yield
    finally:
        core.active_auth_providers.clear()
        core.active_auth_providers.update(saved)


@pytest.fixture
def principal_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    store: dict[str, bool] = {}

    async def _list_principals() -> list[dict[str, Any]]:
        return [{"user_id": user_id, "disabled": disabled} for user_id, disabled in store.items()]

    monkeypatch.setattr(members_ops.management, "list_principals", _list_principals)
    return store


def _as_admin(monkeypatch: pytest.MonkeyPatch, *modules: object) -> None:
    async def _admin() -> Caller:
        return Caller(caller_id="admin1", policy=AccessPolicy(scopes=["*"]), is_admin=True, owner_claim=None)

    for module in modules:
        monkeypatch.setattr(module, "resolve_caller", _admin)


def _as_non_admin(monkeypatch: pytest.MonkeyPatch, *modules: object) -> None:
    async def _editor() -> Caller:
        return Caller(caller_id="editor1", policy=AccessPolicy(scopes=["read"]), is_admin=False, owner_claim=None)

    for module in modules:
        monkeypatch.setattr(module, "resolve_caller", _editor)


def _register(name: str, provider: AccountsProvider) -> None:
    from tai42_skeleton.app.instance import app

    app._serving_core.active_auth_providers[name] = provider


def _empty() -> MemberListing:
    return MemberListing(members=[], invites=[])


async def test_catalog_lists_declared_actions_with_opaque_keys_and_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_admin(monkeypatch, member_actions_ops)
    _register("neutral", _NeutralActionsProvider(_empty()))

    catalog = await member_actions_ops.list_member_actions()

    by_action = {_decode_action_key(d.key)[1]: d for d in catalog.actions}
    assert set(by_action) == {"ping", "poke"}
    # Opaque keys route back to the producing provider.
    assert all(_decode_action_key(d.key)[0] == "neutral" for d in catalog.actions)
    ping = by_action["ping"]
    # The label is the provider's inline content, resolved by the REAL resource manager — the
    # content= path a real plugin uses (a plugin cannot seed a stored id=).
    assert ping.label == "Ping the member"
    assert ping.scope == "page"
    assert ping.destructive is False
    assert ping.input_schema["properties"]["note"]["type"] == "string"
    assert ping.result_schema["properties"]["echo"]["type"] == "string"
    poke = by_action["poke"]
    # Inline Jinja in the content is rendered by the real manager (proving it is not a passthrough).
    assert poke.label == "Poke member 2"
    assert poke.scope == "member_row"
    assert poke.destructive is True


async def test_directory_rows_carry_handle_and_matching_action_keys(
    monkeypatch: pytest.MonkeyPatch, principal_store: dict[str, bool]
) -> None:
    _as_admin(monkeypatch, member_actions_ops, members_ops)
    principal_store["usr-1"] = False
    _register("neutral", _NeutralActionsProvider(MemberListing(members=[_member("usr-1")], invites=[])))

    directory = await members_ops.list_members()
    catalog = await member_actions_ops.list_member_actions()

    row = directory.members[0]
    assert _decode_handle(row.handle) == ("neutral", "usr-1")
    # The row's opaque action keys are exactly the catalog keys for its applicable actions.
    assert row.action_keys == [_encode_action_key("neutral", "poke")]
    catalog_keys = {d.key for d in catalog.actions}
    assert set(row.action_keys) <= catalog_keys


async def test_invoke_happy_path_routes_validates_and_returns_opaque_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_admin(monkeypatch, member_actions_ops)
    provider = _NeutralActionsProvider(_empty())
    _register("neutral", provider)

    result = await member_actions_ops.invoke_member_action(
        action_key=_encode_action_key("neutral", "poke"),
        target_handle=_encode_handle("neutral", "usr-1"),
        input={"reason": "because"},
    )

    # Routed to the neutral provider with the decoded target and the validated payload.
    assert provider.invoked[0][0] == "poke"
    assert provider.invoked[0][1] == "usr-1"
    assert isinstance(provider.invoked[0][2], _PokeInput)
    # The platform carries the result opaquely — the provider's result_model dump.
    assert result.result == {"link": "link:usr-1:because"}


async def test_invoke_page_action_has_no_target(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_admin(monkeypatch, member_actions_ops)
    provider = _NeutralActionsProvider(_empty())
    _register("neutral", provider)

    result = await member_actions_ops.invoke_member_action(
        action_key=_encode_action_key("neutral", "ping"),
        target_handle=None,
        input={"note": "hi"},
    )

    assert provider.invoked[0][1] is None
    assert result.result == {"echo": "hi"}


async def test_invoke_input_type_error_is_422_with_field_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_admin(monkeypatch, member_actions_ops)
    _register("neutral", _NeutralActionsProvider(_empty()))

    with pytest.raises(ValidationRejectedError) as excinfo:
        await member_actions_ops.invoke_member_action(
            action_key=_encode_action_key("neutral", "ping"),
            target_handle=None,
            input={"note": 123, "unexpected": "x"},  # wrong type on a declared field
        )
    # The 422 carries the offending field path, never the rejected value.
    errors = cast("list[dict[str, Any]]", excinfo.value.extra["error"])
    assert any(entry["loc"] == ["note"] for entry in errors)


async def test_invoke_ignores_unknown_input_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # The operator's ruling: the per-action input model uses pydantic's default extra
    # behaviour, so an unknown key is accepted and dropped (never refused).
    _as_admin(monkeypatch, member_actions_ops)
    provider = _NeutralActionsProvider(_empty())
    _register("neutral", provider)

    result = await member_actions_ops.invoke_member_action(
        action_key=_encode_action_key("neutral", "ping"),
        target_handle=None,
        input={"note": "hi", "surprise": "ignored"},
    )

    payload = provider.invoked[0][2]
    assert isinstance(payload, _PingInput)
    assert not hasattr(payload, "surprise")
    assert result.result == {"echo": "hi"}


async def test_invoke_maps_the_providers_contract_error_to_status_never_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A contract-only provider (which cannot import the operations errors) raises the generic
    # member-action error family; the invoke op maps each kind to the matching typed operation
    # error for its status — never a generic 500.
    cases: list[tuple[MemberActionError, type[OperationError], int]] = [
        (MemberActionError("bad input"), ValidationRejectedError, 422),
        (MemberActionNotFoundError("no such target"), NotFoundError, 404),
        (MemberActionConflictError("email already taken"), ConflictError, 409),
        (MemberActionBadRequestError("malformed"), BadRequestError, 400),
    ]
    for raised, expected_type, expected_status in cases:
        _as_admin(monkeypatch, member_actions_ops)
        _register("neutral", _NeutralActionsProvider(_empty(), invoke_error=raised))

        with pytest.raises(expected_type) as excinfo:
            await member_actions_ops.invoke_member_action(
                action_key=_encode_action_key("neutral", "poke"),
                target_handle=_encode_handle("neutral", "usr-1"),
                input={"reason": "x"},
            )
        # The right status, and the provider's message surfaced unchanged.
        assert excinfo.value.status == expected_status
        assert excinfo.value.status != 500
        assert str(raised) == excinfo.value.message


async def test_invoke_unknown_action_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.operations import NotFoundError

    _as_admin(monkeypatch, member_actions_ops)
    _register("neutral", _NeutralActionsProvider(_empty()))

    with pytest.raises(NotFoundError):
        await member_actions_ops.invoke_member_action(
            action_key=_encode_action_key("neutral", "nope"),
            target_handle=None,
            input={},
        )


async def test_catalog_and_invoke_refuse_a_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_non_admin(monkeypatch, member_actions_ops)
    _register("neutral", _NeutralActionsProvider(_empty()))

    with pytest.raises(ForbiddenError):
        await member_actions_ops.list_member_actions()
    with pytest.raises(ForbiddenError):
        await member_actions_ops.invoke_member_action(
            action_key=_encode_action_key("neutral", "ping"), target_handle=None, input={"note": "x"}
        )


async def test_invoke_rejects_a_malformed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_admin(monkeypatch, member_actions_ops)
    _register("neutral", _NeutralActionsProvider(_empty()))

    with pytest.raises(BadRequestError):
        await member_actions_ops.invoke_member_action(action_key="not-base64!!", target_handle=None, input={})
    with pytest.raises(BadRequestError):
        await member_actions_ops.invoke_member_action(
            action_key=_encode_action_key("neutral", "ping"), target_handle="also-bad!!", input={"note": "x"}
        )


async def test_invoke_rejects_key_and_handle_naming_different_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_admin(monkeypatch, member_actions_ops)
    _register("neutral", _NeutralActionsProvider(_empty()))

    with pytest.raises(BadRequestError):
        await member_actions_ops.invoke_member_action(
            action_key=_encode_action_key("neutral", "poke"),
            target_handle=_encode_handle("other", "usr-1"),
            input={"reason": "x"},
        )


async def test_join_derives_disabled_only_when_every_principal_is_disabled(
    monkeypatch: pytest.MonkeyPatch, principal_store: dict[str, bool]
) -> None:
    _as_admin(monkeypatch, members_ops)
    principal_store.update({"p-all-a": True, "p-all-b": True, "p-mixed-a": True, "p-mixed-b": False})
    _register(
        "neutral",
        _NeutralActionsProvider(
            MemberListing(
                members=[
                    _member("all-off", principal_ids=["p-all-a", "p-all-b"]),
                    _member("mixed", principal_ids=["p-mixed-a", "p-mixed-b"]),
                ],
                invites=[],
            )
        ),
    )

    directory = await members_ops.list_members()
    by_id = {row.id: row for row in directory.members}

    all_off = by_id["all-off"]
    assert [p.disabled for p in all_off.principals] == [True, True]
    assert all_off.disabled is True
    mixed = by_id["mixed"]
    assert [(p.user_id, p.disabled) for p in mixed.principals] == [("p-mixed-a", True), ("p-mixed-b", False)]
    # A person with any enabled principal can still sign in — the derived badge stays active.
    assert mixed.disabled is False


async def test_join_raises_loud_on_a_dangling_principal_id(
    monkeypatch: pytest.MonkeyPatch, principal_store: dict[str, bool]
) -> None:
    from tai42_skeleton.operations import OperationFailedError

    _as_admin(monkeypatch, members_ops)
    # The store holds no principal for the id the provider names.
    _register(
        "neutral",
        _NeutralActionsProvider(MemberListing(members=[_member("ghost", principal_ids=["missing-pid"])], invites=[])),
    )

    with pytest.raises(OperationFailedError, match="missing-pid"):
        await members_ops.list_members()
