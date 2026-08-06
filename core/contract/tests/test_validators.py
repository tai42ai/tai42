"""Validator behavior tests — exercise the REAL logic in the contract: the
pydantic field/model validators (and ``model_post_init`` invariants).

Each invariant gets a PASS path (a valid instance constructs) and one or more
RAISE paths (an invalid instance is rejected). Validator ``ValueError``s are
wrapped by pydantic into ``ValidationError``, which subclasses ``ValueError``;
``model_post_init`` raises a bare ``ValueError``. Both are caught with
``pytest.raises(ValueError)``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError

from tai42_contract.backend import CallbackSchema
from tai42_contract.connectors.models import (
    ConnectionRecord,
    ConnectorRef,
    PatchSubServicesRequest,
    StartConnectRequest,
    StartReconnectRequest,
)
from tai42_contract.connectors.providers import (
    ConfigFieldSpec,
    McpServerDescriptor,
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)
from tai42_contract.interactions.models import (
    MEDIA_CAPTION_MAX_CHARS,
    MEDIA_DATA_URI_MAX_CHARS,
    MEDIA_MAX_ITEMS,
    MEDIA_TOTAL_URI_CHARS,
    MEDIA_URL_MAX_CHARS,
    AnswerFormat,
    InteractionRequest,
    MediaItem,
    MediaKind,
)
from tai42_contract.manifest import AgentsConfig, ApiToolsConfig, Manifest, MCPConfig, TaiMCPConfig, ToolsConfig
from tai42_contract.monitoring.models import MonitoringFilter

UUID = "12345678-1234-1234-1234-123456789ABC"


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


# === connectors/models.py ===================================================


def _now() -> datetime:
    return datetime.now(UTC)


def _oauth_record(**overrides: Any) -> ConnectionRecord:
    base: dict[str, Any] = {
        "connection_id": UUID,
        "provider_id": "google",
        "alias": "my-google",
        "kind": "oauth",
        "account_identity": "me@example.com",
        "enabled_sub_services": ["gmail"],
        "access_token": "a-token",
        "refresh_token": "r-token",
        "access_token_expires_at": _now(),
        "created_at": _now(),
    }
    base.update(overrides)
    return ConnectionRecord(**base)


def _noauth_record(**overrides: Any) -> ConnectionRecord:
    base: dict[str, Any] = {
        "connection_id": UUID,
        "provider_id": "local",
        "alias": "files",
        "kind": "none",
        "enabled_sub_services": ["files"],
        "config_values": {"api_key": "v"},
        "created_at": _now(),
    }
    base.update(overrides)
    return ConnectionRecord(**base)


def test_record_connection_id_normalized_lowercase():
    assert _oauth_record().connection_id == UUID.lower()


def test_record_connection_id_invalid_raises():
    with pytest.raises(ValueError, match="not a valid UUID"):
        _oauth_record(connection_id="not-a-uuid")


def test_record_provider_id_slug_invalid_raises():
    with pytest.raises(ValueError, match="lowercase"):
        _oauth_record(provider_id="Google")


def test_record_alias_invalid_raises():
    with pytest.raises(ValueError, match="alias must be"):
        _oauth_record(alias="-bad")


def test_record_enabled_sub_services_slug_invalid_raises():
    with pytest.raises(ValueError, match="lowercase"):
        _oauth_record(enabled_sub_services=["Bad"])


def test_record_naive_datetime_raises():
    with pytest.raises(ValueError, match="timezone-aware"):
        _oauth_record(created_at=datetime(2026, 1, 1))


def test_record_datetime_converted_to_utc():
    plus2 = timezone(timedelta(hours=2))
    rec = _oauth_record(created_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus2))
    assert rec.created_at.tzinfo == UTC
    assert rec.created_at.hour == 10


def test_record_oauth_valid():
    assert _oauth_record().kind == "oauth"


def test_record_oauth_requires_tokens():
    with pytest.raises(ValueError, match="access \\+ refresh tokens"):
        _oauth_record(access_token=None)


def test_record_oauth_requires_account_identity():
    with pytest.raises(ValueError, match="requires account_identity"):
        _oauth_record(account_identity=None)


def test_record_oauth_requires_expires_at():
    with pytest.raises(ValueError, match="access_token_expires_at"):
        _oauth_record(access_token_expires_at=None)


def test_record_oauth_forbids_config_values():
    with pytest.raises(ValueError, match="must not carry config_values"):
        _oauth_record(config_values={"k": "v"})


def test_record_noauth_valid():
    assert _noauth_record().kind == "none"


def test_record_noauth_forbids_tokens_identity_expiry():
    with pytest.raises(ValueError, match="must not carry tokens"):
        _noauth_record(access_token="x")
    with pytest.raises(ValueError, match="must not carry tokens"):
        _noauth_record(account_identity="me@x.com")
    with pytest.raises(ValueError, match="must not carry tokens"):
        _noauth_record(access_token_expires_at=_now())


# -- ConnectorRef ------------------------------------------------------------


def test_connector_ref_valid():
    ref = ConnectorRef(connection_id=UUID, provider_id="google", sub_service="gmail")
    assert ref.connection_id == UUID.lower()


def test_connector_ref_uuid_invalid_raises():
    with pytest.raises(ValueError, match="not a valid UUID"):
        ConnectorRef(connection_id="nope", provider_id="google", sub_service="gmail")


def test_connector_ref_slug_invalid_raises():
    with pytest.raises(ValueError, match="lowercase"):
        ConnectorRef(connection_id=UUID, provider_id="Google", sub_service="gmail")
    with pytest.raises(ValueError, match="lowercase"):
        ConnectorRef(connection_id=UUID, provider_id="google", sub_service="Gmail")


# === interactions/models.py — InteractionRequest._check_payload ============


def _interaction(**overrides: Any) -> InteractionRequest:
    base: dict[str, Any] = {
        "interaction_id": "i1",
        "group_id": "g1",
        "question": "?",
        "reply_to": "ch",
        "created_at": _now(),
        "timeout_at": _now(),
    }
    base.update(overrides)
    return InteractionRequest(**base)


def test_interaction_select_valid():
    req = _interaction(answer_format=AnswerFormat.SELECT, format_payload={"options": ["a", "b"]})
    assert req.answer_format is AnswerFormat.SELECT


def test_interaction_select_requires_options():
    with pytest.raises(ValueError, match="non-empty options"):
        _interaction(answer_format=AnswerFormat.SELECT, format_payload={"options": []})
    with pytest.raises(ValueError, match="non-empty options"):
        _interaction(answer_format=AnswerFormat.SELECT, format_payload=None)


def test_interaction_form_valid():
    req = _interaction(answer_format=AnswerFormat.FORM, format_payload={"schema": {"type": "object"}})
    assert req.answer_format is AnswerFormat.FORM


def test_interaction_form_requires_schema():
    with pytest.raises(ValueError, match="requires a schema"):
        _interaction(answer_format=AnswerFormat.FORM, format_payload={})


def test_interaction_text_valid_no_payload():
    assert _interaction(answer_format=AnswerFormat.TEXT).format_payload is None


def test_interaction_text_forbids_payload():
    with pytest.raises(ValueError, match="carries no format_payload"):
        _interaction(answer_format=AnswerFormat.TEXT, format_payload={"x": 1})


def test_interaction_confirm_forbids_payload():
    with pytest.raises(ValueError, match="carries no format_payload"):
        _interaction(answer_format=AnswerFormat.CONFIRM, format_payload={"x": 1})


def test_interaction_external_valid():
    req = _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload={"url": "https://sign.example/abc"})
    assert req.answer_format is AnswerFormat.EXTERNAL


def test_interaction_external_requires_url():
    # Missing, empty, and non-str url are all rejected.
    with pytest.raises(ValueError, match="non-empty string url"):
        _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload={})
    with pytest.raises(ValueError, match="non-empty string url"):
        _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload={"url": ""})
    with pytest.raises(ValueError, match="non-empty string url"):
        _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload={"url": 123})
    with pytest.raises(ValueError, match="non-empty string url"):
        _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload=None)


def test_interaction_channel_defaults_none_and_round_trips():
    assert _interaction().channel is None
    req = _interaction(channel="telegram")
    assert req.channel == "telegram"
    assert InteractionRequest.model_validate(req.model_dump()).channel == "telegram"


def test_interaction_audience_defaults_none_and_json_round_trips():
    # Omitting audience yields None, and that None survives a
    # model_dump_json/model_validate_json round-trip (the store persists via
    # model_dump_json).
    assert _interaction().audience is None
    default = InteractionRequest.model_validate_json(_interaction().model_dump_json())
    assert default.audience is None


def test_interaction_audience_explicit_json_round_trips():
    # An explicit audience user_id serializes to JSON and reloads unchanged.
    req = _interaction(audience="user-42")
    assert req.audience == "user-42"
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.audience == "user-42"


def test_interaction_recipient_defaults_none_and_json_round_trips():
    # Omitting recipient yields None, surviving the store's model_dump_json round-trip.
    assert _interaction().recipient is None
    default = InteractionRequest.model_validate_json(_interaction().model_dump_json())
    assert default.recipient is None


def test_interaction_recipient_explicit_json_round_trips():
    # An explicit delivery address serializes to JSON and reloads unchanged.
    req = _interaction(recipient="+15551234567")
    assert req.recipient == "+15551234567"
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.recipient == "+15551234567"


def test_interaction_origin_defaults_none_and_json_round_trips():
    # Omitting origin yields None, surviving the store's model_dump_json round-trip.
    assert _interaction().origin is None
    default = InteractionRequest.model_validate_json(_interaction().model_dump_json())
    assert default.origin is None


def test_interaction_origin_explicit_json_round_trips():
    # An explicit run origin serializes to JSON and reloads unchanged.
    req = _interaction(origin="run-abc123")
    assert req.origin == "run-abc123"
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.origin == "run-abc123"


# === interactions/models.py — MediaItem + InteractionRequest.media ==========


_DATA_IMAGE = "data:image/png;base64,iVBORw0KGgo="


# -- MediaItem valid forms ---------------------------------------------------


def test_media_image_https_valid():
    item = MediaItem(kind=MediaKind.IMAGE, url="https://host/x.png")
    assert item.kind is MediaKind.IMAGE
    assert item.caption is None


def test_media_image_data_uri_valid():
    item = MediaItem(kind=MediaKind.IMAGE, url=_DATA_IMAGE)
    assert item.url == _DATA_IMAGE


def test_media_link_https_valid():
    item = MediaItem(kind=MediaKind.LINK, url="https://shop.example/p/1", caption="Buy")
    assert item.caption == "Buy"


def test_media_link_http_valid():
    # http is valid for LINKS (anchors are not governed by img-src).
    item = MediaItem(kind=MediaKind.LINK, url="http://host/path")
    assert item.url == "http://host/path"


def test_media_caption_present_and_absent_valid():
    assert MediaItem(kind=MediaKind.IMAGE, url="https://h/x.png", caption="alt").caption == "alt"
    assert MediaItem(kind=MediaKind.IMAGE, url="https://h/x.png").caption is None


def test_media_request_mixed_eight_items_valid():
    items: list[dict[str, Any]] = []
    for i in range(MEDIA_MAX_ITEMS):
        if i % 2 == 0:
            items.append({"kind": "image", "url": f"https://host/{i}.png"})
        else:
            items.append({"kind": "link", "url": f"https://host/link/{i}", "caption": f"l{i}"})
    req = _interaction(media=items)
    assert req.media is not None
    assert len(req.media) == MEDIA_MAX_ITEMS


def test_media_dict_items_coerce_to_mediaitem():
    req = _interaction(media=[{"kind": "image", "url": "https://h/x.png", "caption": "c"}])
    assert req.media is not None
    assert isinstance(req.media[0], MediaItem)
    assert req.media[0].kind is MediaKind.IMAGE


# -- MediaItem rejected forms ------------------------------------------------


def test_media_image_rejects_javascript_url():
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="javascript:alert(1)")


def test_media_link_rejects_javascript_url():
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="javascript:alert(1)")


def test_media_image_rejects_http_url():
    # Remote images are https-only (the inbox CSP img-src blocks http:).
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="http://host/x.png")


def test_media_link_rejects_data_uri():
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url=_DATA_IMAGE)


def test_media_image_rejects_non_image_data_uri():
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="data:text/html,<h1>x</h1>")
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="data:application/pdf;base64,AAAA")


def test_media_rejects_relative_path_for_both_kinds():
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="/api/storage/resources/1/content")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="/api/storage/resources/1/content")


def test_media_rejects_empty_and_whitespace_url():
    with pytest.raises(ValueError, match="non-blank"):
        MediaItem(kind=MediaKind.IMAGE, url="")
    with pytest.raises(ValueError, match="non-blank"):
        MediaItem(kind=MediaKind.LINK, url="   ")


def test_media_rejects_url_with_empty_netloc():
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://")


def test_media_rejects_userinfo_in_url():
    # ``https://trusted.com@evil.com`` resolves to host evil.com while displaying a
    # trusted-looking authority — an embedded ``user@`` credential form is rejected
    # as a spoofing vector, for both kinds.
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://trusted.com@evil.com/x.png")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://trusted.com@evil.com/p")


def test_media_rejects_hostless_userinfo_url():
    # ``https://user@`` has a non-empty netloc but no host; it is rejected (the
    # check requires a real hostname, not merely a non-empty authority).
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://user@")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://user:pass@")


def test_media_rejects_malformed_authority_with_rule_message():
    # An unterminated IPv6 literal makes urlsplit raise; the validator fails closed
    # with its own rule-naming message rather than leaking the parser's exception.
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://[")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://[")


def test_media_rejects_interior_whitespace_and_control_chars():
    # urlsplit strips \t\r\n before parsing, so an embedded newline/tab would let
    # the validated string diverge from the stored one — reject it up front.
    with pytest.raises(ValueError, match="no whitespace or control characters"):
        MediaItem(kind=MediaKind.IMAGE, url="https://ho\nst/x.png")
    with pytest.raises(ValueError, match="no whitespace or control characters"):
        MediaItem(kind=MediaKind.LINK, url="https://ho st/x")
    # A bidi override inside the host survives urlsplit; reject it as a spoofing vector.
    with pytest.raises(ValueError, match="no whitespace or control characters"):
        MediaItem(kind=MediaKind.LINK, url="https://ev‮il.com/p")


def test_media_rejects_unknown_kind():
    # A dict item with an unknown kind is rejected when it coerces through MediaItem.
    with pytest.raises(ValueError, match="'link'"):
        _interaction(media=[{"kind": "video", "url": "https://h/x.mp4"}])


def test_media_image_data_uri_over_cap_raises():
    url = "data:image/png;base64," + "A" * MEDIA_DATA_URI_MAX_CHARS
    with pytest.raises(ValueError, match=f"data: URI must be at most {MEDIA_DATA_URI_MAX_CHARS}"):
        MediaItem(kind=MediaKind.IMAGE, url=url)


def test_media_https_url_over_cap_raises():
    url = "https://host/" + "a" * MEDIA_URL_MAX_CHARS
    with pytest.raises(ValueError, match=f"url must be at most {MEDIA_URL_MAX_CHARS}"):
        MediaItem(kind=MediaKind.IMAGE, url=url)


def test_media_link_url_over_cap_raises():
    url = "https://host/" + "a" * MEDIA_URL_MAX_CHARS
    with pytest.raises(ValueError, match=f"link media url must be at most {MEDIA_URL_MAX_CHARS}"):
        MediaItem(kind=MediaKind.LINK, url=url)


def test_media_blank_caption_raises():
    with pytest.raises(ValueError, match="caption must be non-blank"):
        MediaItem(kind=MediaKind.IMAGE, url="https://h/x.png", caption="   ")


def test_media_caption_over_cap_raises():
    with pytest.raises(ValueError, match=f"caption must be at most {MEDIA_CAPTION_MAX_CHARS}"):
        MediaItem(kind=MediaKind.IMAGE, url="https://h/x.png", caption="c" * (MEDIA_CAPTION_MAX_CHARS + 1))


def test_media_exact_cap_boundaries_accepted():
    # The caps are strict ``>``; a value of EXACTLY the cap is accepted (guards a
    # ``>`` -> ``>=`` regression).
    at_cap_url = "https://host/" + "a" * (MEDIA_URL_MAX_CHARS - len("https://host/"))
    assert len(at_cap_url) == MEDIA_URL_MAX_CHARS
    assert MediaItem(kind=MediaKind.IMAGE, url=at_cap_url).url == at_cap_url

    prefix = "data:image/png;base64,"
    at_cap_data = prefix + "A" * (MEDIA_DATA_URI_MAX_CHARS - len(prefix))
    assert len(at_cap_data) == MEDIA_DATA_URI_MAX_CHARS
    assert MediaItem(kind=MediaKind.IMAGE, url=at_cap_data).url == at_cap_data

    at_cap_caption = "c" * MEDIA_CAPTION_MAX_CHARS
    assert MediaItem(kind=MediaKind.LINK, url="https://h/x", caption=at_cap_caption).caption == at_cap_caption


# -- InteractionRequest.media list rules -------------------------------------


def test_media_empty_list_raises():
    with pytest.raises(ValueError, match="non-empty list"):
        _interaction(media=[])


def test_media_over_max_items_raises():
    items = [{"kind": "image", "url": f"https://host/{i}.png"} for i in range(MEDIA_MAX_ITEMS + 1)]
    with pytest.raises(ValueError, match=f"at most {MEDIA_MAX_ITEMS} items"):
        _interaction(media=items)


def test_media_total_uri_budget_raises():
    # Each item is within MEDIA_DATA_URI_MAX_CHARS, but the summed url text
    # exceeds the per-question MEDIA_TOTAL_URI_CHARS budget.
    per_item = "data:image/png;base64," + "A" * 400_000
    assert len(per_item) <= MEDIA_DATA_URI_MAX_CHARS
    items = [{"kind": "image", "url": per_item} for _ in range(3)]
    assert sum(len(i["url"]) for i in items) > MEDIA_TOTAL_URI_CHARS
    with pytest.raises(ValueError, match=f"total url length must be at most {MEDIA_TOTAL_URI_CHARS}"):
        _interaction(media=items)


def test_media_total_uri_budget_at_cap_accepted():
    # The total budget is a strict ``>``; a list summing to EXACTLY
    # MEDIA_TOTAL_URI_CHARS is accepted (guards a ``>`` -> ``>=`` regression).
    prefix = "data:image/png;base64,"
    half = MEDIA_TOTAL_URI_CHARS // 2
    url_a = prefix + "A" * (half - len(prefix))
    url_b = prefix + "B" * (MEDIA_TOTAL_URI_CHARS - half - len(prefix))
    items = [{"kind": "image", "url": url_a}, {"kind": "image", "url": url_b}]
    assert sum(len(i["url"]) for i in items) == MEDIA_TOTAL_URI_CHARS
    req = _interaction(media=items)
    assert req.media is not None
    assert len(req.media) == 2


# -- round-trip + orthogonality ----------------------------------------------


def test_media_defaults_none_and_json_round_trips():
    # Omitting media yields None, and an explicit ``"media": null`` reloads as None.
    assert _interaction().media is None
    default = InteractionRequest.model_validate_json(_interaction().model_dump_json())
    assert default.media is None


def test_media_absent_field_loads_as_none():
    # A serialized request whose JSON omits the ``media`` key loads with media None:
    # a missing field reads as the None default, so a record lacking the key is valid.
    payload = json.loads(_interaction().model_dump_json())
    del payload["media"]
    assert "media" not in payload
    assert InteractionRequest.model_validate_json(json.dumps(payload)).media is None


def test_media_json_round_trips_exactly():
    req = _interaction(
        media=[
            {"kind": "image", "url": "https://h/x.png", "caption": "alt"},
            {"kind": "image", "url": _DATA_IMAGE},
            {"kind": "link", "url": "https://shop/p/1"},
        ]
    )
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.media == req.media
    assert restored.media is not None
    assert restored.media[1].caption is None


@pytest.mark.parametrize(
    ("answer_format", "format_payload"),
    [
        (AnswerFormat.TEXT, None),
        (AnswerFormat.CONFIRM, None),
        (AnswerFormat.SELECT, {"options": ["a", "b"]}),
        (AnswerFormat.FORM, {"schema": {"type": "object"}}),
        (AnswerFormat.EXTERNAL, {"url": "https://sign.example/abc"}),
    ],
)
def test_media_orthogonal_to_every_answer_format(answer_format: AnswerFormat, format_payload: Any):
    req = _interaction(
        answer_format=answer_format,
        format_payload=format_payload,
        media=[{"kind": "image", "url": "https://h/x.png"}],
        sensitive=True,
        channel="telegram",
        audience="user-1",
    )
    assert req.media is not None
    assert req.format_payload == format_payload
    assert req.sensitive is True
    assert req.channel == "telegram"
    assert req.audience == "user-1"


# === manifest — MCPConfig ===================================================


# -- normalize_dict_values (before) ------------------------------------------


def test_mcpconfig_dict_none_becomes_empty():
    cfg = MCPConfig(command="run", env=cast("dict[str, str]", None))
    assert cfg.env == {}


def test_mcpconfig_dict_stringifies_values():
    # Intentionally passes non-str values; the validator stringifies them.
    cfg = MCPConfig(command="run", env=cast("dict[str, str]", {"A": 1, "B": 2}))
    assert cfg.env == {"A": "1", "B": "2"}


def test_mcpconfig_dict_none_value_raises_naming_key():
    # A None value is malformed: fail loud naming the key, never silently drop it.
    with pytest.raises(ValueError, match="'B' must not be None"):
        MCPConfig(command="run", env=cast("dict[str, str]", {"A": 1, "B": None}))


def test_mcpconfig_dict_non_dict_raises():
    with pytest.raises(ValueError, match="Expected a dictionary"):
        # Intentionally passes a non-dict to exercise the validator's type guard.
        MCPConfig(command="run", env=cast("dict[str, str]", "not-a-dict"))


# -- empty-transport normalization + args None-normalization -----------------


def test_mcpconfig_empty_transport_normalizes_to_none():
    # An empty transport string is "not set": normalized to None so the gate and
    # the is_* predicates agree.
    cfg = MCPConfig(uds="")
    assert cfg.uds is None
    assert MCPConfig(url="").url is None
    assert MCPConfig(command="").command is None


def test_mcpconfig_args_none_normalizes_to_empty_list():
    cfg = MCPConfig(command="run", args=cast("list[str]", None))
    assert cfg.args == []


# -- _exactly_one_transport --------------------------------------------------


def test_mcpconfig_zero_transport_valid():
    assert MCPConfig().url is None


def test_mcpconfig_single_transport_valid():
    assert MCPConfig(url="https://x").url == "https://x"


def test_mcpconfig_two_transports_raise():
    with pytest.raises(ValueError, match="exactly one of url/uds/command"):
        MCPConfig(url="https://x", command="run")


def test_mcpconfig_url_with_args_raises():
    with pytest.raises(ValueError, match="``args`` is launcher-only"):
        MCPConfig(url="https://x", args=["a"])


def test_mcpconfig_url_with_env_raises():
    with pytest.raises(ValueError, match="``env`` is launcher-only"):
        MCPConfig(url="https://x", env={"A": "1"})


def test_mcpconfig_command_with_headers_raises():
    with pytest.raises(ValueError, match="``headers`` is HTTP-only"):
        MCPConfig(command="run", headers={"H": "v"})


def test_mcpconfig_command_with_args_env_valid():
    cfg = MCPConfig(command="run", args=["a"], env={"A": "1"})
    assert cfg.command == "run"


def test_mcpconfig_url_with_headers_valid():
    cfg = MCPConfig(url="https://x", headers={"H": "v"})
    assert cfg.headers == {"H": "v"}


# === manifest — ExtensionsConfigMixin.normalize_extensions ==================


def _tools_cfg(**overrides: Any) -> ToolsConfig:
    base: dict[str, Any] = {"title": "t", "module": "m"}
    base.update(overrides)
    return ToolsConfig(**base)


def test_extensions_flat_value_wraps_as_single_combo():
    # {weather: [chain]} normalizes to a single combo {"weather": [["chain"]]}.
    cfg = _tools_cfg(extensions={"weather": ["chain"]})
    assert cfg.extensions == {"weather": [["chain"]]}


def test_extensions_list_of_combos_kept_unchanged():
    # {report: [[chain], [chain, batch]]} stays two combos.
    cfg = _tools_cfg(extensions={"report": [["chain"], ["chain", "batch"]]})
    assert cfg.extensions == {"report": [["chain"], ["chain", "batch"]]}


def test_extensions_default_empty():
    assert _tools_cfg().extensions == {}


def test_extensions_mixed_value_raises_naming_key():
    with pytest.raises(ValueError, match="'x'"):
        _tools_cfg(extensions={"x": ["chain", ["batch"]]})


def test_extensions_non_list_value_raises_naming_key():
    with pytest.raises(ValueError, match=r"'x'.*must be a list"):
        _tools_cfg(extensions={"x": "chain"})


def test_extensions_empty_list_value_raises_naming_key():
    with pytest.raises(ValueError, match=r"'x'.*must not be empty"):
        _tools_cfg(extensions={"x": []})


def test_extensions_empty_inner_combo_raises_naming_key():
    with pytest.raises(ValueError, match=r"'x'.*combo must not be empty"):
        _tools_cfg(extensions={"x": [[]]})


def test_extensions_non_string_member_raises_naming_key():
    # A combo member that is neither a name nor a {"name", "config"} mapping (here
    # a bare int) is rejected loudly, naming the key.
    with pytest.raises(ValueError, match=r"'x'.*extension name or a"):
        _tools_cfg(extensions={"x": [["chain", 1]]})


def test_extensions_non_dict_raises():
    with pytest.raises(ValueError, match="extensions must be a mapping"):
        _tools_cfg(extensions="not-a-dict")


# -- {"name", "config"} combo elements (author-bound config) ------------------


def test_extensions_dict_element_in_flat_combo_binds_config():
    # A flat combo may mix a bare name and a {"name", "config"} mapping; the
    # mapping is wrapped into the single combo unchanged.
    cfg = _tools_cfg(extensions={"sign": [{"name": "ask_external", "config": {"verifier": {"name": "gh"}}}, "monitor"]})
    assert cfg.extensions == {"sign": [[{"name": "ask_external", "config": {"verifier": {"name": "gh"}}}, "monitor"]]}


def test_extensions_dict_element_in_list_of_combos():
    combos = [[{"name": "ask_external", "config": {"verifier": {"name": "gh"}}}], ["monitor"]]
    cfg = _tools_cfg(extensions={"sign": combos})
    assert cfg.extensions == {"sign": combos}


def test_extensions_dict_element_missing_name_raises():
    with pytest.raises(ValueError, match="non-empty string 'name'"):
        _tools_cfg(extensions={"x": [{"config": {}}]})


def test_extensions_dict_element_non_dict_config_raises():
    with pytest.raises(ValueError, match="must carry a 'config' mapping"):
        _tools_cfg(extensions={"x": [{"name": "ask_external", "config": "nope"}]})


def test_extensions_dict_element_missing_config_raises():
    with pytest.raises(ValueError, match="must carry a 'config' mapping"):
        _tools_cfg(extensions={"x": [{"name": "ask_external"}]})


def test_extensions_dict_element_extra_key_raises():
    with pytest.raises(ValueError, match="unexpected keys"):
        _tools_cfg(extensions={"x": [{"name": "ask_external", "config": {}, "bogus": 1}]})


def test_extensions_independent_of_include():
    # include (selection) and extensions (attachment) are independent: a config
    # with BOTH a non-empty include and an extensions map validates, and each
    # keeps its own shape (include stays a plain name list).
    cfg = _tools_cfg(include=["weather", "report"], extensions={"weather": ["chain"]})
    assert cfg.include == ["weather", "report"]
    assert cfg.exclude == []
    assert cfg.extensions == {"weather": [["chain"]]}


def test_taimcpconfig_accepts_extensions():
    # The mixin sits on TaiMCPConfig too: an MCP config carries the map.
    cfg = TaiMCPConfig(
        title="t",
        config=MCPConfig(url="https://x"),
        extensions=cast("dict[str, Any]", {"search": ["chain"]}),
    )
    assert cfg.extensions == {"search": [["chain"]]}


def test_agents_config_extensions_key_raises_extra_field():
    # extra="forbid": an extensions key on an agents config is a loud pydantic
    # extra-field error, never silently ignored (the mixin is NOT on AgentsConfig).
    kwargs: dict[str, Any] = {"title": "t", "module": "m", "extensions": {"x": ["chain"]}}
    with pytest.raises(ValueError, match=r"[Ee]xtra"):
        AgentsConfig(**kwargs)


# === manifest — ApiToolsConfig ==============================================


def test_api_tools_defaults_no_args():
    # Default construction (what Manifest's default_factory calls) succeeds and
    # yields the locked default-in shape.
    cfg = ApiToolsConfig()
    assert cfg.enabled is True
    assert cfg.expose_destructive is True
    assert cfg.include == []
    assert cfg.exclude == []
    assert cfg.extensions == {}


def test_api_tools_include_only():
    cfg = ApiToolsConfig(include=["reload_config", "list_hooks"])
    assert cfg.include == ["reload_config", "list_hooks"]
    assert cfg.exclude == []


def test_api_tools_exclude_only():
    cfg = ApiToolsConfig(exclude=["remove_tool"])
    assert cfg.exclude == ["remove_tool"]
    assert cfg.include == []


def test_api_tools_include_exclude_overlap_raises():
    # An op in BOTH lists is a loud validation error — the deliberate deviation
    # from BaseConfig semantics, unique to this config.
    with pytest.raises(ValueError, match=r"both include and exclude"):
        ApiToolsConfig(include=["reload_config"], exclude=["reload_config"])


def test_api_tools_extensions_map_round_trips():
    # The mixin's extensions map is carried and normalized like any other config,
    # and survives a model_dump/model_validate round-trip.
    cfg = ApiToolsConfig(extensions=cast("dict[str, Any]", {"reload_config": [["cache"]]}))
    assert cfg.extensions == {"reload_config": [["cache"]]}
    assert ApiToolsConfig.model_validate(cfg.model_dump()).extensions == {"reload_config": [["cache"]]}


def test_manifest_api_tools_defaults_and_survives_model_dump():
    # api_tools is a NORMAL serialized field: absent input materializes a default
    # ApiToolsConfig, and it survives model_dump so live_manifest carries it.
    m = Manifest()
    assert isinstance(m.api_tools, ApiToolsConfig)
    assert m.api_tools.enabled is True
    dumped = m.model_dump()
    assert dumped["api_tools"] == {
        "enabled": True,
        "expose_destructive": True,
        "include": [],
        "exclude": [],
        "extensions": {},
    }


def test_manifest_api_tools_round_trips_when_present():
    m = Manifest(api_tools=ApiToolsConfig(enabled=False, exclude=["remove_tool"]))
    restored = Manifest.model_validate(m.model_dump())
    assert restored.api_tools.enabled is False
    assert restored.api_tools.exclude == ["remove_tool"]


def test_manifest_unknown_top_level_key_raises_naming_it():
    # extra="forbid": an unknown top-level manifest key is a misconfig and must
    # fail loudly naming the offending key, not be silently dropped.
    with pytest.raises(ValidationError, match="not_a_real_key"):
        Manifest.model_validate({"not_a_real_key": 1})


# === monitoring — MonitoringFilter._check_ranges ============================


def test_monitoring_filter_valid_ranges():
    f = MonitoringFilter(min_cost=1.0, max_cost=2.0, min_tokens=1, max_tokens=2, min_latency=0.1, max_latency=0.2)
    assert f.min_cost == 1.0


def test_monitoring_filter_empty_valid():
    assert MonitoringFilter().min_cost is None


def test_monitoring_filter_cost_inverted_raises():
    with pytest.raises(ValueError, match="min_cost"):
        MonitoringFilter(min_cost=2.0, max_cost=1.0)


def test_monitoring_filter_tokens_inverted_raises():
    with pytest.raises(ValueError, match="min_tokens"):
        MonitoringFilter(min_tokens=10, max_tokens=5)


def test_monitoring_filter_latency_inverted_raises():
    with pytest.raises(ValueError, match="min_latency"):
        MonitoringFilter(min_latency=2.0, max_latency=1.0)


# === manifest — Manifest ``*_modules`` fields ===============================


def test_manifest_modules_default_to_empty_lists():
    m = Manifest()
    assert m.middlewares_modules == []
    assert m.routers_modules == []
    assert m.extensions_modules == []
    assert m.lifecycle_modules == []
    assert m.webhook_verifier_modules == []
    assert m.channel_modules == []
    assert m.studio_plugins == []


def test_manifest_studio_plugins_survive_model_dump():
    # studio_plugins is a NORMAL serialized field (no exclude) so the registry
    # can read it back off live_manifest's model_dump output.
    m = Manifest(studio_plugins=["acme_plugin"])
    assert m.model_dump()["studio_plugins"] == ["acme_plugin"]


@pytest.mark.parametrize(
    "field",
    [
        "middlewares_modules",
        "routers_modules",
        "extensions_modules",
        "lifecycle_modules",
        "webhook_verifier_modules",
        "channel_modules",
        "studio_plugins",
    ],
)
def test_manifest_modules_reject_explicit_null(field: str):
    kwargs: dict[str, Any] = {field: None}
    with pytest.raises(ValueError, match="valid list"):
        Manifest(**kwargs)


# === backend/callback.py — CallbackSchema.tool is optional ==================


def test_callback_schema_tool_defaults_empty():
    # No tool set: the backend runs the rendered expr directly.
    assert CallbackSchema().tool == ""
    assert CallbackSchema(tool="do_thing").tool == "do_thing"


# === connectors — request models cap enabled_sub_services at 64 =============


def _build_start_connect(subs: list[str]) -> BaseModel:
    return StartConnectRequest(provider_id="google", alias="a", enabled_sub_services=subs)


def _build_start_reconnect(subs: list[str]) -> BaseModel:
    return StartReconnectRequest(enabled_sub_services=subs)


def _build_patch_sub_services(subs: list[str]) -> BaseModel:
    return PatchSubServicesRequest(enabled_sub_services=subs)


@pytest.mark.parametrize(
    "build",
    [_build_start_connect, _build_start_reconnect, _build_patch_sub_services],
)
def test_request_enabled_sub_services_length_bounds(build: Callable[[list[str]], BaseModel]):
    # min_length=1 and max_length=64 both enforced at the request layer, so an
    # oversize list is rejected with 422 instead of exploding at record build.
    assert build([f"s{i}" for i in range(64)])
    with pytest.raises(ValueError, match="at most 64"):
        build([f"s{i}" for i in range(65)])
    with pytest.raises(ValueError, match="at least 1"):
        build([])


# === interactions — InteractionRequest naive-datetime rejection =============


def test_interaction_rejects_naive_created_at():
    with pytest.raises(ValueError, match="timezone-aware"):
        _interaction(created_at=datetime(2026, 1, 1))


def test_interaction_rejects_naive_timeout_at():
    with pytest.raises(ValueError, match="timezone-aware"):
        _interaction(timeout_at=datetime(2026, 1, 1))


def test_interaction_datetime_normalized_to_utc():
    plus2 = timezone(timedelta(hours=2))
    req = _interaction(created_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus2))
    assert req.created_at.tzinfo == UTC
    assert req.created_at.hour == 10
