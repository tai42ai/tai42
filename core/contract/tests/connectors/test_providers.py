"""Validator tests for ``connectors/providers.py`` — the MCP server descriptor, config
field spec, sub-service descriptor, and the provider-kind invariants (``_check_kind_invariants``)."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_contract.connectors.providers import (
    ConfigFieldSpec,
    McpServerDescriptor,
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)

# === connectors/providers.py ================================================


# -- McpServerDescriptor._check_url ------------------------------------------


def test_mcp_url_valid_schemes_pass():
    for url in ("http://x", "https://x", "ws://x", "wss://x"):
        d = McpServerDescriptor(type="http", url=url)
        assert d.url == url


def test_mcp_url_empty_normalizes_to_none():
    # _check_url returns None for falsy input; type stays the http default which
    # then requires a url -> post_init raises. So drive the empty branch via a
    # stdio descriptor where url is legitimately unset.
    d = McpServerDescriptor(type="stdio", command="run", url="")
    assert d.url is None


def test_mcp_url_bad_scheme_raises():
    with pytest.raises(ValueError, match="http"):
        McpServerDescriptor(type="http", url="ftp://x")


# -- McpServerDescriptor.model_post_init -------------------------------------


def test_mcp_stdio_valid():
    d = McpServerDescriptor(type="stdio", command="run", args=["--x"], env={"A": "1"})
    assert d.command == "run"


def test_mcp_stdio_requires_command():
    with pytest.raises(ValueError, match="stdio MCP server requires command"):
        McpServerDescriptor(type="stdio")


def test_mcp_stdio_forbids_url():
    with pytest.raises(ValueError, match="must not set url/extra_headers"):
        McpServerDescriptor(type="stdio", command="run", url="http://x")


def test_mcp_stdio_forbids_extra_headers():
    with pytest.raises(ValueError, match="must not set url/extra_headers"):
        McpServerDescriptor(type="stdio", command="run", extra_headers={"H": "v"})


def test_mcp_http_valid():
    d = McpServerDescriptor(type="http", url="https://x", extra_headers={"H": "v"})
    assert d.url == "https://x"


def test_mcp_http_requires_url():
    with pytest.raises(ValueError, match="http MCP server requires url"):
        McpServerDescriptor(type="http")


def test_mcp_http_forbids_command_args_env():
    with pytest.raises(ValueError, match="must not set command/args/env"):
        McpServerDescriptor(type="http", url="https://x", command="run")


# -- ConfigFieldSpec._check_key ----------------------------------------------


def test_config_field_key_valid():
    f = ConfigFieldSpec(key="api_key", label="API Key", target="env")
    assert f.key == "api_key"


def test_config_field_key_invalid_raises():
    with pytest.raises(ValueError, match="config field key"):
        ConfigFieldSpec(key="API_KEY", label="x", target="env")


# -- SubServiceDescriptor._check_slug + _check_launch_spec -------------------


def _stdio_sub(sub_id: str = "files") -> SubServiceDescriptor:
    return SubServiceDescriptor(
        id=sub_id,
        display_name="Files",
        mcp_server=McpServerDescriptor(type="stdio", command="run"),
    )


def test_sub_service_slug_valid():
    assert _stdio_sub().id == "files"


def test_sub_service_slug_invalid_raises():
    with pytest.raises(ValueError, match="sub-service id"):
        SubServiceDescriptor(
            id="Bad-Id",
            display_name="x",
            mcp_server=McpServerDescriptor(type="stdio", command="run"),
        )


def test_sub_service_launch_mcp_server_only_valid():
    assert _stdio_sub().mcp_server is not None


def test_sub_service_launch_entry_point_only_valid():
    s = SubServiceDescriptor(id="gmail", display_name="Gmail", entry_point="tai-mcp-x")
    assert s.entry_point == "tai-mcp-x"


def test_sub_service_launch_neither_raises():
    with pytest.raises(ValueError, match="exactly one of mcp_server / entry_point"):
        SubServiceDescriptor(id="files", display_name="x")


def test_sub_service_launch_both_raises():
    with pytest.raises(ValueError, match="exactly one of mcp_server / entry_point"):
        SubServiceDescriptor(
            id="files",
            display_name="x",
            entry_point="tai-mcp-x",
            mcp_server=McpServerDescriptor(type="stdio", command="run"),
        )


# -- ProviderDescriptor ------------------------------------------------------


def _oauth_sub(sub_id: str = "gmail") -> SubServiceDescriptor:
    return SubServiceDescriptor(
        id=sub_id,
        display_name="Gmail",
        scopes=["scope.read"],
        mcp_server=McpServerDescriptor(type="http", url="https://m"),
    )


def _oauth_provider(**overrides: Any) -> ProviderDescriptor:
    base: dict[str, Any] = {
        "id": "google",
        "display_name": "Google",
        "icon_url": "https://i/icon.png",
        "kind": "oauth",
        "origin": "system",
        "category": "productivity",
        "oauth": OAuthEndpoints(authorize="https://a", token="https://t"),
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
        "sub_services": {"gmail": _oauth_sub()},
    }
    base.update(overrides)
    return ProviderDescriptor(**base)


def _noauth_provider(**overrides: Any) -> ProviderDescriptor:
    base: dict[str, Any] = {
        "id": "local",
        "display_name": "Local",
        "icon_url": "https://i/icon.png",
        "kind": "none",
        "origin": "system",
        "category": "dev",
        "sub_services": {"files": _stdio_sub()},
    }
    base.update(overrides)
    return ProviderDescriptor(**base)


def test_provider_id_valid():
    assert _oauth_provider().id == "google"


def test_provider_id_invalid_raises():
    with pytest.raises(ValueError, match="provider id"):
        _oauth_provider(id="Google")


def test_provider_icon_url_accepts_https():
    assert _oauth_provider(icon_url="https://cdn.example.com/mark.png").icon_url == "https://cdn.example.com/mark.png"


@pytest.mark.parametrize(
    "bad",
    [
        "http://cdn.example.com/mark.png",  # not https
        "/static/connector-icons/mark.svg",  # relative, no scheme/host
        "mark.png",  # bare relative path
        "",  # empty
    ],
)
def test_provider_icon_url_rejects_non_https(bad: str):
    with pytest.raises(ValueError, match="icon_url"):
        _oauth_provider(icon_url=bad)


def test_provider_sub_services_empty_raises():
    with pytest.raises(ValueError, match="at least one sub-service"):
        _oauth_provider(sub_services={})


def test_provider_sub_services_key_mismatch_raises():
    with pytest.raises(ValueError, match=r"must match sub\.id"):
        _oauth_provider(sub_services={"wrong": _oauth_sub("gmail")})


# pkg_manager-required-when-entry_point rule


def test_provider_entry_point_requires_pkg_manager_raises():
    sub = SubServiceDescriptor(id="gmail", display_name="Gmail", scopes=["s"], entry_point="tai-mcp-x")
    with pytest.raises(ValueError, match="requires pkg_manager"):
        _oauth_provider(sub_services={"gmail": sub}, pkg_manager=None)


def test_provider_entry_point_with_pkg_manager_valid():
    sub = SubServiceDescriptor(id="gmail", display_name="Gmail", scopes=["s"], entry_point="tai-mcp-x")
    p = _oauth_provider(sub_services={"gmail": sub}, pkg_manager="uvx")
    assert p.pkg_manager == "uvx"


# oauth kind invariants


def test_provider_oauth_valid():
    assert _oauth_provider().kind == "oauth"


def test_provider_oauth_requires_endpoints():
    with pytest.raises(ValueError, match="requires oauth endpoints"):
        _oauth_provider(oauth=None)


def test_provider_oauth_requires_client_envs():
    with pytest.raises(ValueError, match="client_id_env"):
        _oauth_provider(client_id_env=None)
    with pytest.raises(ValueError, match="client_id_env"):
        _oauth_provider(client_secret_env=None)


def test_provider_oauth_forbids_config_fields():
    with pytest.raises(ValueError, match="must not declare config_fields"):
        _oauth_provider(config_fields=[ConfigFieldSpec(key="k", label="K", target="header")])


def test_provider_oauth_requires_nonempty_scopes():
    sub = SubServiceDescriptor(
        id="gmail",
        display_name="Gmail",
        scopes=[],
        mcp_server=McpServerDescriptor(type="http", url="https://m"),
    )
    with pytest.raises(ValueError, match="scopes must be non-empty"):
        _oauth_provider(sub_services={"gmail": sub})


# no-auth kind invariants


def test_provider_noauth_valid():
    assert _noauth_provider().kind == "none"


def test_provider_noauth_forbids_oauth():
    with pytest.raises(ValueError, match="must not set oauth endpoints"):
        _noauth_provider(oauth=OAuthEndpoints(authorize="https://a", token="https://t"))


def test_provider_noauth_forbids_client_creds():
    with pytest.raises(ValueError, match="must not set client creds"):
        _noauth_provider(client_id_env="X")
    with pytest.raises(ValueError, match="must not set client creds"):
        _noauth_provider(client_secret_env="X")


def test_provider_noauth_config_fields_unique_keys():
    fields = [
        ConfigFieldSpec(key="token", label="T", target="env"),
        ConfigFieldSpec(key="token", label="T2", target="env"),
    ]
    with pytest.raises(ValueError, match="keys must be unique"):
        _noauth_provider(config_fields=fields)


def test_provider_noauth_config_fields_single_channel_env_valid():
    # stdio sub -> env channel; matching config_field target passes.
    p = _noauth_provider(config_fields=[ConfigFieldSpec(key="token", label="T", target="env")])
    assert p.config_fields[0].target == "env"


def test_provider_noauth_config_fields_header_channel_valid():
    sub = SubServiceDescriptor(
        id="api",
        display_name="API",
        mcp_server=McpServerDescriptor(type="http", url="https://m"),
    )
    p = _noauth_provider(
        sub_services={"api": sub},
        config_fields=[ConfigFieldSpec(key="token", label="T", target="header")],
    )
    assert p.config_fields[0].target == "header"


def test_provider_noauth_mcp_none_treated_as_stdio_channel():
    # entry_point sub leaves mcp_server None -> detected as the stdio/env channel.
    sub = SubServiceDescriptor(id="files", display_name="Files", entry_point="tai-mcp-x")
    p = _noauth_provider(
        sub_services={"files": sub},
        pkg_manager="uvx",
        config_fields=[ConfigFieldSpec(key="token", label="T", target="env")],
    )
    assert p.config_fields[0].target == "env"


def test_provider_noauth_mixed_channels_raises():
    subs = {
        "files": _stdio_sub("files"),
        "api": SubServiceDescriptor(
            id="api",
            display_name="API",
            mcp_server=McpServerDescriptor(type="http", url="https://m"),
        ),
    }
    with pytest.raises(ValueError, match="one transport channel"):
        _noauth_provider(
            sub_services=subs,
            config_fields=[ConfigFieldSpec(key="token", label="T", target="env")],
        )


def test_provider_noauth_target_channel_mismatch_raises():
    # stdio sub -> env channel, but the field declares header.
    with pytest.raises(ValueError, match=r"must .*match the transport channel"):
        _noauth_provider(config_fields=[ConfigFieldSpec(key="token", label="T", target="header")])
