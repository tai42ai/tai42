"""Client-input validators and account-identity extraction (pure unit tests)."""

from __future__ import annotations

import pytest

from tai42_skeleton.connectors.service.connection_service.complete import _extract_account_identity
from tai42_skeleton.connectors.service.connection_service.validation import (
    _scopes_for,
    _validate_config_values,
    _validate_return_url,
    _validate_sub_services,
)

from ..conftest import make_noauth_stdio_descriptor, make_oauth_descriptor


def test_validate_return_url_accepts_path():
    assert _validate_return_url("/connectors?x=1") == "/connectors?x=1"


@pytest.mark.parametrize("bad", ["//evil.com", "http://x", "no-slash", "/\nbad"])
def test_validate_return_url_rejects_bad(bad):
    with pytest.raises(ValueError, match="same-origin path"):
        _validate_return_url(bad)


def test_validate_sub_services_empty_raises():
    with pytest.raises(ValueError, match="non-empty"):
        _validate_sub_services(make_oauth_descriptor(), [])


def test_scopes_for_unions_and_sorts():
    desc = make_oauth_descriptor()
    assert _scopes_for(desc, ["mail", "cal"]) == ["cal.read", "mail.read", "mail.send"]


def test_validate_config_values_unknown_key():
    desc = make_noauth_stdio_descriptor()
    with pytest.raises(ValueError, match="unknown config values"):
        _validate_config_values(desc, {"nope": "x"})


def test_validate_config_values_missing_required():
    desc = make_noauth_stdio_descriptor()
    with pytest.raises(ValueError, match="missing required"):
        _validate_config_values(desc, {})


def test_extract_account_identity_none_without_id_token():
    assert _extract_account_identity({}) is None


def test_extract_account_identity_from_id_token():
    import base64
    import json

    claims = base64.urlsafe_b64encode(json.dumps({"email": "u@x.test"}).encode()).rstrip(b"=").decode()
    id_token = f"header.{claims}.sig"
    assert _extract_account_identity({"id_token": id_token}) == "u@x.test"


def test_extract_account_identity_wrong_part_count():
    assert _extract_account_identity({"id_token": "only.two"}) is None


def test_extract_account_identity_bad_payload():
    assert _extract_account_identity({"id_token": "h.!!!notb64json!!!.s"}) is None
