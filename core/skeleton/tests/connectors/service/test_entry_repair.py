"""The repair the provider's-own-server check names: a reconnect, or saving a connection's services
again, rewrites exactly the connector's own entries the check refuses, and nothing else."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from tai42_contract.connectors.models import ConnectorRef
from tai42_contract.connectors.providers import McpServerDescriptor, ProviderDescriptor, SubServiceDescriptor
from tai42_contract.connectors.service import FlowOperation
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.providers import registry
from tai42_skeleton.connectors.runtime.resolver import ConnectorConnectionError
from tai42_skeleton.connectors.service.connection_service import complete_connect, patch_sub_services
from tai42_skeleton.connectors.service.manifest_writer import managed_entries_to_write
from tai42_skeleton.connectors.token_injection import require_own_server

from ..conftest import CID, CID2, make_noauth_record, make_noauth_stdio_descriptor, make_oauth_record
from .conftest import ORIGIN, REDIRECT, _flow, _token_response


def _moved(descriptor: ProviderDescriptor, sub: str, url: str) -> ProviderDescriptor:
    """``descriptor`` with sub-service ``sub``'s server moved to ``url``."""
    moved = descriptor.model_copy(deep=True)
    old = moved.sub_services[sub]
    moved.sub_services[sub] = SubServiceDescriptor(
        id=sub,
        display_name=old.display_name,
        scopes=list(old.scopes),
        mcp_server=McpServerDescriptor(type="http", url=url),
    )
    return moved


def _use(providers: dict[str, Any], descriptor: ProviderDescriptor) -> None:
    """Make ``descriptor`` the one the service and the run-time check both read."""
    providers[descriptor.id] = descriptor
    registry.reset_registry()
    for each in providers.values():
        registry.register_connector(each)


def _entry(doc: dict[str, Any], title: str) -> TaiMCPConfig:
    return TaiMCPConfig.model_validate(next(e for e in doc["mcp"] if e["title"] == title))


async def _reconnect(harness, monkeypatch, *, enabled: list[str]) -> None:
    _cs, _records, _store, flows, _events, _providers = harness
    flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=CID)
    flow.enabled_sub_services = enabled
    flows[flow.flow_id] = flow

    async def fake_exchange(**_kwargs):
        return _token_response(access_token="new-at")

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    await complete_connect(flow_id=flow.flow_id, code="auth")


def _refusal(title: str, provider: str, sub: str, repair: str) -> str:
    """The text the own-server check gives for entry ``title`` whose url is not ``provider``'s for ``sub``."""
    return (
        f"MCP entry {title!r} carries the reference of connection {CID} but its url is not the one provider "
        f"{provider!r} defines for sub-service {sub!r}, so no credential is sent to it; {repair}"
    )


_RECONNECT = (
    "reconnect the account to rewrite the entry the connector wrote for that connection, "
    "or remove the reference from this entry if it is not that one"
)
_SAVE_SERVICES = (
    "save the connection's services again to rewrite the entry the connector wrote for that connection, "
    "or remove the reference from this entry if it is not that one"
)


def _refused_with(config: TaiMCPConfig) -> str:
    with pytest.raises(ConnectorConnectionError) as caught:
        require_own_server(config)
    return str(caught.value)


def _pipeline(harness):
    cs, *_ = harness
    return cs.ConfigService.from_app()


async def test_a_reconnect_rewrites_the_connectors_own_entry_that_fell_behind(harness, monkeypatch):
    _cs, records, _store, _flows, _events, providers = harness
    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail"])
    pipeline = _pipeline(harness)
    pipeline.seed(descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID)

    _use(providers, _moved(providers["acme"], "mail", "https://new.acme.test/mcp/mail"))
    assert _refused_with(_entry(pipeline.doc, "acme_mail_work")) == _refusal(
        "acme_mail_work", "acme", "mail", _RECONNECT
    )

    await _reconnect(harness, monkeypatch, enabled=["mail"])

    rewritten = _entry(pipeline.doc, "acme_mail_work")
    assert rewritten.config.url == "https://new.acme.test/mcp/mail"
    assert require_own_server(rewritten) is providers["acme"]
    assert pipeline.applies == 1  # the reconnect's one apply (the seed writes the document directly)
    opened = await _call_once_through_the_seam(monkeypatch, rewritten)
    assert opened["config"]["url"] == "https://new.acme.test/mcp/mail"
    assert opened["config"]["headers"]["authorization"] == "Bearer at"


async def _call_once_through_the_seam(monkeypatch, config: TaiMCPConfig) -> dict:
    """Make one call of ``config`` through the call seam and return the config its request opened."""
    import mcp.types

    import tai42_skeleton.connectors.token_injection as ti
    from tai42_skeleton.connectors.runtime.resolver import ManagedAuth

    async def fake_resolve(cid, pid, ss, *, allow_refresh=True):
        return ManagedAuth(access_token="at")

    monkeypatch.setattr(ti, "resolve_managed_auth", fake_resolve)
    opened: list[dict] = []

    class _Session:
        async def call_tool_mcp(self, *args, **kwargs):
            return mcp.types.CallToolResult(content=[], isError=False)

    class _Pool:
        @asynccontextmanager
        async def current(self, *, config):
            opened.append(config)
            yield _Session()

    async def run(client, meta):
        return await client.call_tool_mcp("send", {}, meta=meta)

    await ti.with_managed_auth(config, "http", cast("Any", _Pool()), run, tool_name="send")
    return opened[0]


async def test_a_reconnect_leaves_another_entry_carrying_the_reference_untouched(harness, monkeypatch):
    _cs, records, _store, _flows, _events, providers = harness
    _use(providers, providers["acme"])
    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail"])
    pipeline = _pipeline(harness)
    pipeline.seed(descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID)
    forged = TaiMCPConfig(
        title="borrowed",
        config=MCPConfig(type="http", url="https://elsewhere.test/mcp"),
        managed=ConnectorRef(connection_id=CID, provider_id="acme", sub_service="mail"),
    )
    pipeline.doc["mcp"].append(forged.model_dump(mode="json", exclude_none=True))
    told = _refusal("borrowed", "acme", "mail", _RECONNECT)
    assert _refused_with(forged) == told

    await _reconnect(harness, monkeypatch, enabled=["mail"])

    assert _entry(pipeline.doc, "borrowed") == forged
    assert _refused_with(_entry(pipeline.doc, "borrowed")) == told


async def test_an_entry_titled_like_another_aliass_entry_is_refused_with_both_repairs_and_left_alone_by_a_reconnect(
    harness, monkeypatch
):
    _cs, records, _store, _flows, _events, providers = harness
    _use(providers, providers["acme"])
    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail"])
    pipeline = _pipeline(harness)
    pipeline.seed(descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID)
    lookalike = TaiMCPConfig(
        title="acme_mail_home",
        config=MCPConfig(type="http", url="https://elsewhere.test/mcp"),
        managed=ConnectorRef(connection_id=CID, provider_id="acme", sub_service="mail"),
    )
    pipeline.doc["mcp"].append(lookalike.model_dump(mode="json", exclude_none=True))
    told = _refusal("acme_mail_home", "acme", "mail", _RECONNECT)
    assert _refused_with(lookalike) == told

    await _reconnect(harness, monkeypatch, enabled=["mail"])

    assert _entry(pipeline.doc, "acme_mail_home") == lookalike
    assert _refused_with(_entry(pipeline.doc, "acme_mail_home")) == told


async def test_an_entry_equal_to_the_builders_output_causes_no_manifest_write(harness, monkeypatch):
    _cs, records, _store, _flows, _events, providers = harness
    _use(providers, providers["acme"])
    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail"])
    pipeline = _pipeline(harness)
    pipeline.seed(descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID)

    await _reconnect(harness, monkeypatch, enabled=["mail"])

    assert pipeline.applies == 0


async def test_saving_an_unchanged_set_rewrites_a_no_auth_connections_entry(harness):
    _cs, records, _store, _flows, _events, providers = harness
    records[CID] = make_noauth_record(
        connection_id=CID, provider_id="httpsvc", alias="main", enabled_sub_services=["main"], config_values={}
    )
    pipeline = _pipeline(harness)
    pipeline.seed(descriptor=providers["httpsvc"], enabled_sub_services=["main"], alias="main", connection_id=CID)
    _use(providers, _moved(providers["httpsvc"], "main", "https://new.httpsvc.test/mcp"))
    assert _refused_with(_entry(pipeline.doc, "httpsvc_main_main")) == _refusal(
        "httpsvc_main_main", "httpsvc", "main", _SAVE_SERVICES
    )

    result = await patch_sub_services(
        connection_id=CID, desired=["main"], return_url="/x", redirect_uri=REDIRECT, origin=ORIGIN
    )

    assert result.added_manifest_entries == ["httpsvc_main_main"]
    assert _entry(pipeline.doc, "httpsvc_main_main").config.url == "https://new.httpsvc.test/mcp"
    assert require_own_server(_entry(pipeline.doc, "httpsvc_main_main")) is not None


async def test_saving_an_unchanged_set_with_nothing_refused_is_still_refused(harness):
    _cs, records, _store, _flows, _events, providers = harness
    _use(providers, providers["httpsvc"])
    records[CID] = make_noauth_record(
        connection_id=CID, provider_id="httpsvc", alias="main", enabled_sub_services=["main"], config_values={}
    )
    pipeline = _pipeline(harness)
    pipeline.seed(descriptor=providers["httpsvc"], enabled_sub_services=["main"], alias="main", connection_id=CID)
    with pytest.raises(ValueError, match="unchanged"):
        await patch_sub_services(
            connection_id=CID, desired=["main"], return_url="/x", redirect_uri=REDIRECT, origin=ORIGIN
        )
    assert pipeline.applies == 0


async def test_a_stdio_entry_keeps_its_package_version_through_a_save_and_a_reconnect(harness):
    _cs, records, _store, _flows, _events, providers = harness
    records[CID] = make_noauth_record(connection_id=CID, alias="main", enabled_sub_services=["search"])
    pipeline = _pipeline(harness)
    pipeline.seed(descriptor=providers["widgets"], enabled_sub_services=["search"], alias="main", connection_id=CID)
    written = _entry(pipeline.doc, "widgets_search_main")
    bumped = make_noauth_stdio_descriptor().model_copy(update={"pkg_version": "9.9.9"})
    _use(providers, bumped)

    write = managed_entries_to_write(
        pipeline.doc,
        descriptor=bumped,
        enabled_sub_services=["search"],
        alias="main",
        connection_id=CID,
    )
    assert not write
    with pytest.raises(ValueError, match="unchanged"):
        await patch_sub_services(
            connection_id=CID, desired=["search"], return_url="/x", redirect_uri=REDIRECT, origin=ORIGIN
        )
    assert _entry(pipeline.doc, "widgets_search_main") == written


async def test_a_title_owned_by_another_connection_still_raises(harness, monkeypatch):
    _cs, records, _store, _flows, _events, providers = harness
    _use(providers, providers["acme"])
    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail"])
    pipeline = _pipeline(harness)
    pipeline.seed(descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID2)
    with pytest.raises(ValueError, match="title collision"):
        await _reconnect(harness, monkeypatch, enabled=["mail"])
