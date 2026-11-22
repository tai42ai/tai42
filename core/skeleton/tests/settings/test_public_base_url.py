"""``TAI_PUBLIC_BASE_URL``: the env binding, the normalised value, the refusals and the owned prefix."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tai42_kit.settings import UnknownOwnedSettingError, refuse_unknown_owned_env

from tai42_skeleton.settings.owned_settings import OWNED_SETTINGS_MODULES
from tai42_skeleton.settings.public_base_url import PublicBaseUrlSettings, public_base_url_settings

from .._helpers import set_public_base_url


def test_the_env_name_binds_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    set_public_base_url(monkeypatch, "https://host.example/")
    assert public_base_url_settings().base_url == "https://host.example"


def test_the_public_url_is_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    set_public_base_url(monkeypatch, None)
    assert public_base_url_settings().base_url is None


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("HTTPS://Host.Example:443/x/", "https://host.example/x"),
        ("https://host.example/", "https://host.example"),
        ("https://host.example", "https://host.example"),
        ("https://host.example:8443", "https://host.example:8443"),
        ("https://host.example/Prefix/Path", "https://host.example/Prefix/Path"),
        ("http://localhost:8000/", "http://localhost:8000"),
        ("http://127.0.0.1:80/p", "http://127.0.0.1/p"),
        ("http://[::1]:9000", "http://[::1]:9000"),
    ],
)
def test_the_public_url_is_stored_normalised(value: str, stored: str) -> None:
    assert PublicBaseUrlSettings(base_url=value).base_url == stored


@pytest.mark.parametrize(
    "value",
    [
        "http://host.example",
        "ftp://host.example",
        "https://host.example?x=1",
        "https://host.example/?",
        "https://host.example#f",
        "https://user:pw@host.example",
        "https://hôst.example",
        "https://host.example/x/../y",
        "https://host.example/x/./y",
        "https://host.example/x//y",
        "https://host.example/a b",
        "https://host.example/a%20b",
        "https://host.example:99999",
        "host.example",
    ],
)
def test_a_public_url_a_client_would_rewrite_is_refused_naming_the_setting(value: str) -> None:
    with pytest.raises(ValidationError, match="TAI_PUBLIC_BASE_URL"):
        PublicBaseUrlSettings(base_url=value)


def test_the_group_owns_its_prefix() -> None:
    assert "tai42_skeleton.settings.public_base_url" in OWNED_SETTINGS_MODULES
    assert PublicBaseUrlSettings.env_prefix_owned is True
    refuse_unknown_owned_env({"TAI_PUBLIC_BASE_URL": "https://host.example"}, source="boot")
    with pytest.raises(UnknownOwnedSettingError, match=r"TAI_PUBLIC_BASE_URI \(prefix TAI_PUBLIC_\)"):
        refuse_unknown_owned_env({"TAI_PUBLIC_BASE_URI": "https://host.example"}, source="boot")
