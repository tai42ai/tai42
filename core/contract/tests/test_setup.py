"""Tests for the setup-door request/result shapes (``SetupRequest`` / ``SetupResult``).

These pin the defaults the door mints around (server-minted ids, key description),
the required ``owner_display_name``, the optional discriminated ``login`` credential,
and the once-returned key result — not the door's logic (skeleton).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract import SetupRequest, SetupResult
from tai42_contract.accounts.models import InviteCredential, PasswordCredential


def test_setup_request_defaults():
    request = SetupRequest(owner_display_name="Alice")
    assert request.setup_token == ""
    assert request.owner_user_id is None
    assert request.key_user_id is None
    assert request.key_description == "owner key"
    assert request.login is None


def test_setup_request_requires_owner_display_name():
    with pytest.raises(ValidationError):
        SetupRequest()  # pyright: ignore[reportCallIssue]


def test_setup_request_rejects_an_empty_owner_display_name():
    with pytest.raises(ValidationError):
        SetupRequest(owner_display_name="")


def test_setup_request_forbids_unknown_keys():
    with pytest.raises(ValidationError):
        SetupRequest(owner_display_name="Alice", nonsense=True)  # pyright: ignore[reportCallIssue]


def test_setup_request_accepts_a_password_login():
    request = SetupRequest(
        owner_display_name="Alice",
        login={"kind": "password", "email": "a@x.test", "password": "pw"},  # pyright: ignore[reportArgumentType]
    )
    assert isinstance(request.login, PasswordCredential)


def test_setup_request_accepts_an_invite_login():
    request = SetupRequest(
        owner_display_name="Alice",
        login={"kind": "invite", "email": "a@x.test"},  # pyright: ignore[reportArgumentType]
    )
    assert isinstance(request.login, InviteCredential)


def test_setup_request_rejects_an_unknown_login_kind():
    with pytest.raises(ValidationError):
        SetupRequest(
            owner_display_name="Alice",
            login={"kind": "magic", "email": "a@x.test"},  # pyright: ignore[reportArgumentType]
        )


def test_setup_result_minimal_and_defaults():
    result = SetupResult(
        owner_user_id="usr-1",
        key_user_id="usr-1-key",
        api_key="sk-plaintext",
        key_fingerprint="fp-1",
        login_attached=False,
    )
    assert result.invite_token is None
    assert result.login_path is None


def test_setup_result_forbids_unknown_keys():
    with pytest.raises(ValidationError):
        SetupResult(
            owner_user_id="usr-1",
            key_user_id="usr-1-key",
            api_key="sk-plaintext",
            key_fingerprint="fp-1",
            login_attached=False,
            nonsense=True,  # pyright: ignore[reportCallIssue]
        )
