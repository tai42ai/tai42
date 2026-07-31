"""Tests for the accounts login-method metadata models and the ``provision``
owner-parameter signature lock."""

from __future__ import annotations

import inspect

import pytest
from pydantic import TypeAdapter, ValidationError

from tai42_contract.access_control.identity import ApiKeyIdentityProvider
from tai42_contract.accounts.models import ButtonMethod, FormField, FormMethod, LoginMethod

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


@pytest.mark.parametrize("purpose", ["login", "bootstrap", "invite"])
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


# -- provision signature lock --------------------------------------------------


def test_provision_has_keyword_only_owner_user_id_defaulting_none():
    param = inspect.signature(ApiKeyIdentityProvider.provision).parameters["owner_user_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


# -- OWNER_USER_ID_CLAIM contract lock -----------------------------------------


def test_owner_user_id_claim_value_and_reexport():
    from tai42_contract.access_control import OWNER_USER_ID_CLAIM

    assert OWNER_USER_ID_CLAIM == "owner_user_id"
