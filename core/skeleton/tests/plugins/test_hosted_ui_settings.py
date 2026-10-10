"""``HOSTED_UI_*``: the page addresses of the hosted UI, declared once and read by every carrier."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tai42_contract.app import HostedUiPaths
from tai42_kit.settings import UnknownOwnedSettingError

from tai42_skeleton.app.facets.http import HttpFacet
from tai42_skeleton.app.http import HttpSurface
from tai42_skeleton.app.instance import app as skeleton_app
from tai42_skeleton.plugins.settings import HostedUiSettings, check_hosted_ui_settings, hosted_ui_settings
from tai42_skeleton.settings.owned_settings import require_known_owned_settings

_OVERRIDES = {
    "HOSTED_UI_LOGIN_PATH": "/signin",
    "HOSTED_UI_CLAIM_FRAGMENT_KEY": "token",
    "HOSTED_UI_INVITE_QUERY_KEY": "join",
    "HOSTED_UI_OAUTH_CALLBACK_PATH": "/cb.html",
    "HOSTED_UI_CONNECTORS_PATH": "/landing",
}


@pytest.fixture
def overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _OVERRIDES.items():
        monkeypatch.setenv(key, value)


def test_the_defaults_are_the_hosted_ui_the_platform_ships() -> None:
    settings = HostedUiSettings()
    assert settings.login_path == "/login"
    assert settings.claim_fragment_key == "claim"
    assert settings.invite_query_key == "invite"
    assert settings.oauth_callback_path == "/oauth-bridge.html"
    assert settings.connectors_path == "/connectors"


@pytest.mark.usefixtures("overridden")
def test_every_field_reads_its_env_name() -> None:
    settings = hosted_ui_settings()
    assert (
        settings.login_path,
        settings.claim_fragment_key,
        settings.invite_query_key,
        settings.oauth_callback_path,
        settings.connectors_path,
    ) == ("/signin", "token", "join", "/cb.html", "/landing")


@pytest.mark.parametrize("field", ["LOGIN_PATH", "OAUTH_CALLBACK_PATH", "CONNECTORS_PATH"])
@pytest.mark.parametrize("value", ["//evil.example", "login", "https://elsewhere.example/login"])
def test_a_path_must_be_same_origin(monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    monkeypatch.setenv(f"HOSTED_UI_{field}", value)
    with pytest.raises(ValidationError, match=f"HOSTED_UI_{field} must be a same-origin path"):
        HostedUiSettings()


@pytest.mark.parametrize("field", ["CLAIM_FRAGMENT_KEY", "INVITE_QUERY_KEY"])
@pytest.mark.parametrize("value", ["a=b", "a b", "a&b", "a#b"])
def test_a_key_is_a_bare_word(monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    monkeypatch.setenv(f"HOSTED_UI_{field}", value)
    with pytest.raises(ValidationError, match=rf"HOSTED_UI_{field} must match \[A-Za-z0-9_-\]\+"):
        HostedUiSettings()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("login_path", "HOSTED_UI_LOGIN_PATH must be a same-origin path"),
        ("claim_fragment_key", r"HOSTED_UI_CLAIM_FRAGMENT_KEY must match \[A-Za-z0-9_-\]\+"),
    ],
)
def test_an_empty_value_is_refused(field: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        HostedUiSettings(**{field: ""})


def test_an_unknown_name_under_the_prefix_refuses_boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOSTED_UI_LOGIN_PAGE", "/signin")
    with pytest.raises(UnknownOwnedSettingError, match="HOSTED_UI_LOGIN_PAGE"):
        require_known_owned_settings()


@pytest.mark.usefixtures("overridden")
def test_every_hosted_ui_setting_boots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    require_known_owned_settings()


@pytest.mark.usefixtures("overridden")
def test_the_http_facet_serves_the_declared_paths() -> None:
    surface = HttpSurface(SimpleNamespace())  # type: ignore[arg-type]
    expected = HostedUiPaths(
        login_path="/signin",
        claim_fragment_key="token",
        invite_query_key="join",
        oauth_callback_path="/cb.html",
        connectors_path="/landing",
    )
    assert surface.hosted_ui() == expected
    facet = HttpFacet(SimpleNamespace(_http_surface=surface))  # type: ignore[arg-type]
    assert facet.hosted_ui() == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("CONNECTORS_PATH", "/connectors#mine"),
        ("CONNECTORS_PATH", "/connectors page"),
        ("OAUTH_CALLBACK_PATH", "/oauth-bridge.html#x"),
        ("OAUTH_CALLBACK_PATH", "/cb\tx"),
        ("LOGIN_PATH", "/login#a"),
        ("LOGIN_PATH", "/login page"),
    ],
)
def test_a_path_refuses_a_character_its_composed_address_cannot_keep(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    monkeypatch.setenv(f"HOSTED_UI_{field}", value)
    with pytest.raises(
        ValidationError, match=rf"HOSTED_UI_{field} must be a same-origin path of \[A-Za-z0-9_\\-\./\?=&%\]"
    ):
        HostedUiSettings()


def test_the_login_path_carries_no_query(monkeypatch: pytest.MonkeyPatch) -> None:
    # An invite link appends its own ``?<key>=<token>`` to the login path.
    monkeypatch.setenv("HOSTED_UI_LOGIN_PATH", "/login?x=1")
    with pytest.raises(ValidationError, match=r"HOSTED_UI_LOGIN_PATH must carry no '\?'"):
        HostedUiSettings()


def test_the_connectors_and_callback_paths_may_carry_a_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTED_UI_CONNECTORS_PATH", "/connectors?tab=mine")
    monkeypatch.setenv("HOSTED_UI_OAUTH_CALLBACK_PATH", "/oauth-bridge.html?v=2")
    settings = HostedUiSettings()
    assert settings.connectors_path == "/connectors?tab=mine"
    assert settings.oauth_callback_path == "/oauth-bridge.html?v=2"


def test_a_malformed_value_refuses_boot_naming_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTED_UI_CLAIM_FRAGMENT_KEY", "a b")
    with pytest.raises(ValidationError, match=r"HOSTED_UI_CLAIM_FRAGMENT_KEY must match \[A-Za-z0-9_-\]\+"):
        check_hosted_ui_settings()


def test_the_declaration_is_checked_at_boot_and_at_every_reload() -> None:
    key = "tai42_skeleton.plugins.settings.check_hosted_ui_settings"
    assert skeleton_app._startup_handlers[key] is check_hosted_ui_settings
    assert skeleton_app._reload_handlers[key] is check_hosted_ui_settings
