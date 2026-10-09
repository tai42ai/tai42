"""Tests for the accounts login-method metadata models and the ``provision``
owner-parameter signature lock."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from tai42_contract.access_control.identity import ApiKeyIdentityProvider
from tai42_contract.accounts.models import (
    ButtonMethod,
    FormField,
    FormMethod,
    InviteCredential,
    InviteEntry,
    InviteRow,
    InvokeMemberActionRequest,
    InvokeMemberActionResult,
    LoginAttachment,
    LoginCredential,
    LoginMethod,
    MemberAction,
    MemberActionCatalog,
    MemberActionDescriptor,
    MemberDirectory,
    MemberEntry,
    MemberListing,
    MemberPrincipalState,
    MemberRow,
    PasswordCredential,
)
from tai42_contract.template import TemplatedText

_credential: TypeAdapter[PasswordCredential | InviteCredential] = TypeAdapter(LoginCredential)

_login_method: TypeAdapter[FormMethod | ButtonMethod] = TypeAdapter(LoginMethod)


# -- Discriminated union -------------------------------------------------------


def test_form_shape_yields_form_method():
    method = _login_method.validate_python(
        {
            "shape": "form",
            "id": "password",
            "title": "Sign in",
            "fields": [{"name": "email", "label": "Email"}],
            "submit_path": "/api/login/password",
        }
    )
    assert isinstance(method, FormMethod)


def test_button_shape_yields_button_method():
    method = _login_method.validate_python(
        {
            "shape": "button",
            "id": "oidc",
            "label": "Continue with SSO",
            "href": "/api/login/oidc/start",
        }
    )
    assert isinstance(method, ButtonMethod)


def test_unknown_shape_fails_validation():
    with pytest.raises(ValidationError):
        _login_method.validate_python({"shape": "magic", "id": "x"})


# -- extra="forbid" ------------------------------------------------------------


def test_form_field_forbids_unknown_keys():
    with pytest.raises(ValidationError):
        FormField(name="email", label="Email", nonsense=True)  # pyright: ignore[reportCallIssue]


def test_form_method_forbids_unknown_keys():
    with pytest.raises(ValidationError):
        FormMethod(
            id="password",
            title="Sign in",
            fields=[FormField(name="email", label="Email")],
            submit_path="/api/login/password",
            nonsense=True,  # pyright: ignore[reportCallIssue]
        )


def test_button_method_forbids_unknown_keys():
    with pytest.raises(ValidationError):
        ButtonMethod(
            id="oidc",
            label="SSO",
            href="/api/login/oidc/start",
            nonsense=True,  # pyright: ignore[reportCallIssue]
        )


# -- FormMethod.purpose --------------------------------------------------------


def test_purpose_defaults_to_login():
    method = FormMethod(
        id="password",
        title="Sign in",
        fields=[FormField(name="email", label="Email")],
        submit_path="/api/login/password",
    )
    assert method.purpose == "login"


@pytest.mark.parametrize("purpose", ["login", "invite"])
def test_purpose_accepts_the_closed_enum(purpose: str):
    method = FormMethod(
        id="password",
        title="Sign in",
        purpose=purpose,  # pyright: ignore[reportArgumentType]
        fields=[FormField(name="email", label="Email")],
        submit_path="/api/login/password",
    )
    assert method.purpose == purpose


def test_purpose_rejects_unknown_value():
    with pytest.raises(ValidationError):
        FormMethod(
            id="password",
            title="Sign in",
            purpose="signup",  # pyright: ignore[reportArgumentType]
            fields=[FormField(name="email", label="Email")],
            submit_path="/api/login/password",
        )


def test_fields_may_not_be_empty():
    with pytest.raises(ValidationError):
        FormMethod(id="password", title="Sign in", fields=[], submit_path="/api/login/password")


# -- Same-origin path guard ----------------------------------------------------


_NON_API_TARGETS = [
    "relative",
    "https://evil.example",
    "//evil.example",
    "/login/x",
    "/api",
    "/apix",
    "/api/../admin",
    "/api/../../logout",
    "/api/%2e%2e/admin",
    "/api/.%2e/admin",
    "/api/%2e./admin",
    "/api/%2E%2E/admin",
    "/api/.%2E/admin",
    "/api/%2E./admin",
    "/api\\..\\admin",
    "/api/..\t/evil",
    "/api/.\t./evil",
    "/api/..\n/evil",
    "/api/..\r/evil",
    "/api/.. ",
    "/api/..\x00",
    "/api/..\x1f",
]


@pytest.mark.parametrize("path", _NON_API_TARGETS)
def test_submit_path_rejects_non_api_targets(path: str):
    with pytest.raises(ValidationError):
        FormMethod(
            id="password",
            title="Sign in",
            fields=[FormField(name="email", label="Email")],
            submit_path=path,
        )


def test_submit_path_accepts_api_path():
    method = FormMethod(
        id="password",
        title="Sign in",
        fields=[FormField(name="email", label="Email")],
        submit_path="/api/login/password",
    )
    assert method.submit_path == "/api/login/password"


@pytest.mark.parametrize("href", _NON_API_TARGETS)
def test_href_rejects_non_api_targets(href: str):
    with pytest.raises(ValidationError):
        ButtonMethod(id="oidc", label="SSO", href=href)


def test_href_accepts_api_path():
    method = ButtonMethod(id="oidc", label="SSO", href="/api/login/oidc/start")
    assert method.href == "/api/login/oidc/start"


# -- FormField defaults --------------------------------------------------------


def test_form_field_defaults():
    field = FormField(name="email", label="Email")
    assert field.secret is False
    assert field.autocomplete is None


# -- Login credentials ---------------------------------------------------------


def test_password_credential_kind_and_yields_from_the_union():
    credential = _credential.validate_python({"kind": "password", "email": "a@x.test", "password": "pw"})
    assert isinstance(credential, PasswordCredential)
    assert credential.kind == "password"


def test_invite_credential_kind_and_yields_from_the_union():
    credential = _credential.validate_python({"kind": "invite", "email": "a@x.test"})
    assert isinstance(credential, InviteCredential)
    assert credential.kind == "invite"


def test_credential_unknown_kind_fails_validation():
    with pytest.raises(ValidationError):
        _credential.validate_python({"kind": "magic", "email": "a@x.test"})


def test_password_credential_requires_email_and_password():
    with pytest.raises(ValidationError):
        PasswordCredential(email="a@x.test", password="")  # empty password
    with pytest.raises(ValidationError):
        InviteCredential(email="")  # empty email


def test_login_attachment_defaults():
    attachment = LoginAttachment(attached=False)
    assert attachment.invite_token is None
    assert attachment.login_path is None


# -- Membership listing shapes -------------------------------------------------


def test_member_entry_accepts_an_aware_created_at_and_normalizes_to_utc():
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    entry = MemberEntry(id="usr-1", email="a@x.test", role="editor", created_at=created, principal_ids=["usr-1"])
    assert entry.created_at.tzinfo is UTC
    assert entry.created_at == created


def test_member_entry_rejects_a_naive_created_at():
    with pytest.raises(ValidationError):
        MemberEntry(
            id="usr-1",
            email="a@x.test",
            role="editor",
            created_at=datetime(2026, 1, 2, 3, 4, 5),  # the naive timestamp under test
            principal_ids=["usr-1"],
        )


def test_member_entry_requires_non_empty_id_email_role():
    now = datetime.now(UTC)
    for field, value in (("id", ""), ("email", ""), ("role", "")):
        kwargs = {"id": "usr-1", "email": "a@x.test", "role": "editor", "created_at": now, "principal_ids": ["usr-1"]}
        kwargs[field] = value
        with pytest.raises(ValidationError):
            MemberEntry(**kwargs)  # pyright: ignore[reportArgumentType]


def test_roles_are_nullable_on_every_member_and_invite_shape():
    # ``None``: the principal's policy was not written from a role template.
    now = datetime.now(UTC)
    member = MemberEntry(id="usr-1", email="a@x.test", role=None, created_at=now, principal_ids=["usr-1"])
    invite = InviteEntry(id="usr-2", email="b@x.test", role=None, created_at=now, expires_at=now)
    row = MemberRow(id="usr-1", email="a@x.test", role=None, created_at=now, principals=[], disabled=False, handle="h")
    invite_row = InviteRow(id="usr-2", email="b@x.test", role=None, created_at=now, expires_at=now, handle="h")
    assert (member.role, invite.role, row.role, invite_row.role) == (None, None, None, None)
    for model in (MemberRow, InviteRow, InviteEntry):
        assert model.model_json_schema()["properties"]["role"]["anyOf"][1] == {"type": "null"}


def test_member_entry_requires_at_least_one_principal_id():
    with pytest.raises(ValidationError):
        MemberEntry(id="usr-1", email="a@x.test", role="editor", created_at=datetime.now(UTC), principal_ids=[])


def test_member_entry_defaults_actions_to_empty_and_carries_declared_ids():
    entry = MemberEntry(
        id="usr-1", email="a@x.test", role="editor", created_at=datetime.now(UTC), principal_ids=["usr-1"]
    )
    assert entry.actions == []
    with_actions = MemberEntry(
        id="usr-1",
        email="a@x.test",
        role="editor",
        created_at=datetime.now(UTC),
        principal_ids=["usr-1", "usr-1b"],
        actions=["disable", "remove"],
    )
    assert with_actions.actions == ["disable", "remove"]
    assert with_actions.principal_ids == ["usr-1", "usr-1b"]


def test_member_entry_forbids_extra_fields():
    with pytest.raises(ValidationError):
        MemberEntry(
            id="usr-1",
            email="a@x.test",
            role="editor",
            created_at=datetime.now(UTC),
            principal_ids=["usr-1"],
            surprise="x",  # pyright: ignore[reportCallIssue]
        )


def test_invite_entry_requires_aware_timestamps():
    now = datetime.now(UTC)
    entry = InviteEntry(id="usr-2", email="b@x.test", role="viewer", created_at=now, expires_at=now)
    assert entry.created_at.tzinfo is UTC
    assert entry.expires_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        InviteEntry(
            id="usr-2",
            email="b@x.test",
            role="viewer",
            created_at=now,
            expires_at=datetime(2026, 1, 2, 3, 4, 5),  # the naive timestamp under test
        )


def test_invite_entry_defaults_actions_to_empty_and_carries_declared_ids():
    now = datetime.now(UTC)
    entry = InviteEntry(id="usr-2", email="b@x.test", role="viewer", created_at=now, expires_at=now)
    assert entry.actions == []
    with_actions = InviteEntry(
        id="usr-2", email="b@x.test", role="viewer", created_at=now, expires_at=now, actions=["cancel", "resend"]
    )
    assert with_actions.actions == ["cancel", "resend"]


def test_member_listing_holds_members_and_invites():
    now = datetime.now(UTC)
    listing = MemberListing(
        members=[MemberEntry(id="usr-1", email="a@x.test", role="editor", created_at=now, principal_ids=["usr-1"])],
        invites=[InviteEntry(id="usr-2", email="b@x.test", role="viewer", created_at=now, expires_at=now)],
    )
    assert [m.id for m in listing.members] == ["usr-1"]
    assert [i.id for i in listing.invites] == ["usr-2"]


def test_member_listing_requires_both_lists():
    with pytest.raises(ValidationError):
        MemberListing(members=[])  # pyright: ignore[reportCallIssue]


# -- Member-action declaration shapes ------------------------------------------


class _ActionInput(BaseModel):
    note: str


class _ActionResult(BaseModel):
    link: str


def test_member_action_carries_class_refs_and_is_frozen():
    action = MemberAction(
        id="resend",
        label=TemplatedText(id="resend.label"),
        scope="invite_row",
        input_model=_ActionInput,
        result_model=_ActionResult,
    )
    assert action.input_model is _ActionInput
    assert action.result_model is _ActionResult
    assert action.destructive is False
    with pytest.raises(ValidationError):
        action.id = "other"  # pyright: ignore[reportAttributeAccessIssue]  frozen


def test_member_action_rejects_an_unknown_scope():
    with pytest.raises(ValidationError):
        MemberAction(
            id="x",
            label=TemplatedText(content="x"),
            scope="footer",  # pyright: ignore[reportArgumentType]  not one of the three placements
            input_model=_ActionInput,
            result_model=_ActionResult,
        )


def test_member_action_descriptor_carries_opaque_key_and_rendered_schemas():
    descriptor = MemberActionDescriptor(
        key="opaque-key",
        label="Resend invite",
        scope="invite_row",
        destructive=False,
        input_schema=_ActionInput.model_json_schema(),
        result_schema=_ActionResult.model_json_schema(),
    )
    catalog = MemberActionCatalog(actions=[descriptor])
    assert catalog.actions[0].key == "opaque-key"
    assert catalog.actions[0].input_schema["properties"]["note"]["type"] == "string"


def test_member_row_carries_joined_principals_and_opaque_tokens():
    now = datetime.now(UTC)
    row = MemberRow(
        id="usr-1",
        email="a@x.test",
        role="editor",
        created_at=now,
        principals=[
            MemberPrincipalState(user_id="usr-1", disabled=True),
            MemberPrincipalState(user_id="usr-1b", disabled=False),
        ],
        disabled=False,
        handle="opaque-handle",
        action_keys=["k1", "k2"],
    )
    assert [p.user_id for p in row.principals] == ["usr-1", "usr-1b"]
    assert row.disabled is False
    assert row.handle == "opaque-handle"


def test_invite_row_defaults_action_keys_and_directory_holds_rows():
    now = datetime.now(UTC)
    invite = InviteRow(id="usr-2", email="b@x.test", role="viewer", created_at=now, expires_at=now, handle="h")
    assert invite.action_keys == []
    directory = MemberDirectory(members=[], invites=[invite])
    assert [i.id for i in directory.invites] == ["usr-2"]


def test_invoke_request_ignores_unknown_top_level_keys_and_defaults_input():
    request = InvokeMemberActionRequest.model_validate({"action_key": "k", "surprise": "ignored"})
    assert request.action_key == "k"
    assert request.target_handle is None
    assert request.input == {}
    assert not hasattr(request, "surprise")


def test_invoke_result_forbids_extra_and_carries_opaque_result():
    result = InvokeMemberActionResult(result={"link": "https://x.test/one-time"})
    assert result.result == {"link": "https://x.test/one-time"}
    with pytest.raises(ValidationError):
        InvokeMemberActionResult(result={}, surprise="x")  # pyright: ignore[reportCallIssue]


# -- provision signature lock --------------------------------------------------


def test_provision_owner_user_id_is_keyword_only_and_required():
    param = inspect.signature(ApiKeyIdentityProvider.provision).parameters["owner_user_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    assert param.annotation == "str"


# -- OWNER_USER_ID_CLAIM contract lock -----------------------------------------


def test_owner_user_id_claim_value_and_reexport():
    from tai42_contract.access_control import OWNER_USER_ID_CLAIM

    assert OWNER_USER_ID_CLAIM == "owner_user_id"
