"""The listing seam's edges, the shared server check's edges and the credentials step's bookkeeping.

A network-free listing: ``open_client`` hands back a client whose ``list_tools`` answers by the
bearer the effective config carries, so each step's request is observable.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from tai42_contract.connectors.models import ConnectorRef
from tai42_contract.connectors.providers import McpServerDescriptor, SubServiceDescriptor
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

import tai42_skeleton.connectors.managed_listing as listing_mod
import tai42_skeleton.connectors.runtime.credentials_step as step_mod
import tai42_skeleton.connectors.token_injection as ti
from tai42_skeleton.connectors.providers import registry
from tai42_skeleton.connectors.runtime.own_server import own_server_refusal
from tai42_skeleton.connectors.runtime.resolver import (
    ConnectorConnectionError,
    ConnectorReconnectRequiredError,
    ConnectorRefreshFailingError,
    ManagedAuth,
)

from .conftest import CID, make_oauth_descriptor

URL = "https://acme.test/mcp/mail"


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", URL)
    return httpx.HTTPStatusError(str(status), request=request, response=httpx.Response(status, request=request))


def _entry(*, provider_id: str = "acme", url: str = URL, title: str = "acme_mail_work") -> TaiMCPConfig:
    return TaiMCPConfig(
        title=title,
        config=MCPConfig(type="http", url=url),
        managed=ConnectorRef(connection_id=CID, provider_id=provider_id, sub_service="mail"),
    )


class _Server:
    """Answers each listing by the bearer it carries: ``answers[bearer]`` is the tools or an exception."""

    def __init__(self, answers: dict[str | None, Any]) -> None:
        self.answers = answers
        self.seen: list[str | None] = []

    def open(self, effective: TaiMCPConfig):
        authorization = (effective.config.headers or {}).get("authorization")
        self.seen.append(authorization)
        answer = self.answers.get(authorization, _status_error(401))

        class _Client:
            async def list_tools(self):
                if isinstance(answer, BaseException):
                    raise answer
                return answer

        @asynccontextmanager
        async def _cm():
            yield _Client()

        return _cm()


async def _list(config: TaiMCPConfig, server: _Server) -> Any:
    async def run(client):
        return await client.list_tools()

    return await listing_mod.list_with_managed_auth(config, listing_mod.ManagedListing(open_client=server.open), run)


@pytest.fixture
def acme() -> None:
    registry.reset_registry()
    registry.register_connector(make_oauth_descriptor())


class _Credentials:
    """The resolver as the seam sees it: what a read-only read, a resolve and a forced refresh return."""

    def __init__(self, monkeypatch, *, stored=None, resolved: Any = None, refreshed: Any = None, owner=True) -> None:
        self.calls: list[str] = []

        async def resolve(cid, pid, ss, *, allow_refresh=True):
            self.calls.append("read" if not allow_refresh else "resolve")
            value = stored if not allow_refresh else resolved
            if isinstance(value, BaseException):
                raise value
            return value

        async def force(cid, *, failed_access_token=None):
            self.calls.append(f"refresh {failed_access_token}")
            if isinstance(refreshed, BaseException):
                raise refreshed
            return refreshed

        monkeypatch.setattr(ti, "resolve_managed_auth", resolve)
        monkeypatch.setattr(ti, "force_refresh", force)
        monkeypatch.setattr(listing_mod.refresh_owner, "is_live", lambda: owner)


# -- the listing seam ------------------------------------------------------------------------------


@pytest.mark.usefixtures("acme")
async def test_a_just_obtained_credential_the_server_refuses_names_the_connection(monkeypatch):
    creds = _Credentials(monkeypatch, stored=None, resolved=ManagedAuth(access_token="new"))
    with pytest.raises(listing_mod.ManagedListingRefusedError, match=f"connection {CID}: reconnect") as caught:
        await _list(_entry(), _Server({}))
    assert ti.mcp_http_status(caught.value) == 401
    assert creds.calls == ["read", "resolve"]


@pytest.mark.usefixtures("acme")
async def test_another_failure_with_a_just_obtained_credential_is_raised_as_it_is(monkeypatch):
    _Credentials(monkeypatch, stored=None, resolved=ManagedAuth(access_token="new"))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _list(_entry(), _Server({"Bearer new": _status_error(500)}))
    assert caught.value.response.status_code == 500


@pytest.mark.usefixtures("acme")
async def test_another_failure_with_the_stored_credential_is_raised_as_it_is(monkeypatch):
    creds = _Credentials(monkeypatch, stored=ManagedAuth(access_token="kept"))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _list(_entry(), _Server({"Bearer kept": _status_error(502)}))
    assert caught.value.response.status_code == 502
    assert creds.calls == ["read"]


@pytest.mark.usefixtures("acme")
async def test_a_resolve_that_yields_no_token_fails_with_the_servers_refusal(monkeypatch):
    _Credentials(monkeypatch, stored=None, resolved=None)
    server = _Server({})
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _list(_entry(), server)
    assert caught.value.response.status_code == 401
    assert server.seen == [None]


@pytest.mark.usefixtures("acme")
@pytest.mark.parametrize(
    ("error", "reconnect"),
    [
        (ConnectorReconnectRequiredError("gone", connection_id=CID), True),
        (ConnectorRefreshFailingError("down", connection_id=CID), False),
    ],
)
async def test_a_connector_error_obtaining_a_credential_names_the_connection(monkeypatch, error, reconnect):
    _Credentials(monkeypatch, stored=None, resolved=error)
    with pytest.raises(listing_mod.ManagedListingRefusedError) as caught:
        await _list(_entry(), _Server({}))
    assert CID in str(caught.value)
    assert ("reconnect the account" in str(caught.value)) is reconnect


async def test_an_entry_of_an_unregistered_provider_is_refused_after_the_401(monkeypatch, caplog):
    registry.reset_registry()
    creds = _Credentials(monkeypatch, stored=ManagedAuth(access_token="kept"))
    listing = listing_mod.ManagedListing(open_client=_Server({}).open)

    async def run(client):
        return await client.list_tools()

    with (
        caplog.at_level(logging.ERROR, logger="tai42_skeleton.connectors.token_injection"),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await listing_mod.list_with_managed_auth(_entry(provider_id="unknown"), listing, run)
    assert listing.credential_demanded is True
    assert creds.calls == []
    assert any("is not registered" in record.getMessage() for record in caplog.records)


# -- the shared server check -----------------------------------------------------------------------


_LOOKALIKE_REFUSAL = (
    f"MCP entry 'acme_mail_home' carries the reference of connection {CID} but its url is not the one provider "
    "'acme' defines for sub-service 'mail', so no credential is sent to it; reconnect the account to rewrite the "
    "entry the connector wrote for that connection, or remove the reference from this entry if it is not that one"
)


class _ClosingPool:
    """A pool that records what it is asked to close and refuses to open anything."""

    def __init__(self) -> None:
        self.closed: list[dict[str, Any]] = []

    async def close(self, *, config: dict[str, Any]) -> None:
        self.closed.append(config)

    @asynccontextmanager
    async def current(self, *, config: dict[str, Any]):
        raise AssertionError("a refused entry opens no session")
        yield


async def _door_call(config: TaiMCPConfig) -> None:
    async def run(client, meta):
        return await client.call_tool_mcp("send", {}, meta=meta)

    await ti.with_managed_auth(config, "http", _ClosingPool(), run, tool_name="send")  # type: ignore[arg-type]


async def _door_forced_refresh(config: TaiMCPConfig) -> None:
    await ti._force_refresh(config, "secret")


async def _door_resolve_for_config(config: TaiMCPConfig) -> None:
    await ti.resolve_managed_auth_for_config(config)


async def _door_eviction(config: TaiMCPConfig) -> None:
    pool = _ClosingPool()
    await ti.evict_pooled_session(config, "http", pool)  # type: ignore[arg-type]
    assert pool.closed == [config.model_dump()]


async def _door_listing(config: TaiMCPConfig) -> None:
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _list(config, _Server({}))
    assert caught.value.response.status_code == 401


async def _door_credentials_step(config: TaiMCPConfig) -> None:
    assert await step_mod.bring_credentials_up_to_date([config], timeout=1.0) == {}


@pytest.mark.usefixtures("acme")
@pytest.mark.parametrize(
    ("door", "raises"),
    [
        (_door_call, True),
        (_door_forced_refresh, True),
        (_door_resolve_for_config, True),
        (_door_eviction, False),
        (_door_listing, False),
        (_door_credentials_step, False),
    ],
    ids=["call seam", "forced refresh", "resolve for config", "eviction", "listing", "credentials step"],
)
async def test_every_door_gives_the_one_refusal_and_resolves_nothing(monkeypatch, caplog, door, raises):
    creds = _Credentials(monkeypatch, stored=ManagedAuth(access_token="kept"), resolved=ManagedAuth(access_token="new"))
    monkeypatch.setattr(step_mod.refresh_owner, "is_live", lambda: True)
    monkeypatch.setattr(step_mod, "resolve_managed_auth", ti.resolve_managed_auth)
    lookalike = _entry(url="https://elsewhere.test/mcp", title="acme_mail_home")
    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.connectors.token_injection"):
        if raises:
            with pytest.raises(ConnectorConnectionError) as caught:
                await door(lookalike)
            assert str(caught.value) == _LOOKALIKE_REFUSAL
        else:
            await door(lookalike)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        f"connectors: {_LOOKALIKE_REFUSAL}"
    ]
    assert creds.calls == []


def test_a_descriptor_whose_server_is_not_http_refuses_an_http_entry():
    descriptor = make_oauth_descriptor()
    descriptor.sub_services["mail"] = SubServiceDescriptor(
        id="mail", display_name="Mail", scopes=["mail.read"], mcp_server=McpServerDescriptor(type="stdio", command="x")
    )
    assert own_server_refusal(_entry(), descriptor) == "provider 'acme' defines no http server for sub-service 'mail'"


def test_a_stdio_entry_is_never_refused():
    stdio = TaiMCPConfig(
        title="acme_mail_work",
        config=MCPConfig(type="stdio", command="uvx", args=["acme-mcp==1"]),
        managed=ConnectorRef(connection_id=CID, provider_id="acme", sub_service="mail"),
    )
    assert own_server_refusal(stdio, None) is None


def test_the_check_refuses_to_judge_an_entry_that_is_not_managed():
    with pytest.raises(RuntimeError, match="not connector-managed"):
        own_server_refusal(TaiMCPConfig(title="hand", config=MCPConfig(type="http", url=URL)), None)


# -- the call seam's bookkeeping -------------------------------------------------------------------


def test_a_failing_refresh_becomes_the_refresh_failing_result():
    result = ti.managed_auth_error_result(ConnectorRefreshFailingError("budget spent", connection_id=CID))
    payload = ti.extract_connector_error_payload(result)
    assert payload is not None
    assert payload["code"] == "refresh_failing"
    assert payload["connection_id"] == CID


@pytest.mark.parametrize(("close_error", "level"), [(403, logging.INFO), (500, logging.WARNING)])
async def test_closing_a_session_logs_its_own_refusal_quietly_and_anything_else_loudly(caplog, close_error, level):
    class _Pool:
        async def close(self, *, config):
            raise _status_error(close_error)

    with caplog.at_level(logging.INFO, logger="tai42_skeleton.connectors.token_injection"):
        await ti._close_session(_Pool(), {}, _status_error(403), "under test")  # type: ignore[arg-type]
    assert [r.levelno for r in caplog.records if "pooled MCP session" in r.getMessage()] == [level]


# -- the credentials step's bookkeeping ------------------------------------------------------------


async def test_the_step_does_nothing_without_a_live_owner(monkeypatch, acme):
    monkeypatch.setattr(step_mod.refresh_owner, "is_live", lambda: False)
    assert await step_mod.bring_credentials_up_to_date([_entry()], timeout=1.0) == {}


async def test_the_step_covers_only_managed_oauth_http_entries(monkeypatch, acme):
    monkeypatch.setattr(step_mod.refresh_owner, "is_live", lambda: True)
    reads: list[str] = []

    async def resolve(cid, pid, ss, *, allow_refresh=True):
        reads.append(pid)
        return ManagedAuth(access_token="fresh")

    monkeypatch.setattr(step_mod, "resolve_managed_auth", resolve)
    hand = TaiMCPConfig(title="hand", config=MCPConfig(type="http", url=URL))
    stdio = TaiMCPConfig(
        title="acme_mail_stdio",
        config=MCPConfig(type="stdio", command="uvx", args=["acme-mcp"]),
        managed=ConnectorRef(connection_id=CID, provider_id="acme", sub_service="mail"),
    )
    assert await step_mod.bring_credentials_up_to_date([hand, stdio, _entry()], timeout=1.0) == {"acme_mail_work": True}
    assert reads == ["acme"]


@pytest.mark.parametrize(
    ("late", "logged"), [(RuntimeError("boom"), True), (ConnectorRefreshFailingError("x", connection_id=CID), False)]
)
async def test_a_wrapper_left_behind_has_its_outcome_consumed(monkeypatch, acme, caplog, late, logged):
    monkeypatch.setattr(step_mod.refresh_owner, "is_live", lambda: True)
    release = asyncio.Event()

    async def resolve(cid, pid, ss, *, allow_refresh=True):
        if not allow_refresh:
            return None
        await release.wait()
        raise late

    monkeypatch.setattr(step_mod, "resolve_managed_auth", resolve)
    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.connectors.runtime.credentials_step"):
        assert await step_mod.bring_credentials_up_to_date([_entry()], timeout=0.05) == {"acme_mail_work": False}
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
    after_bound = [r for r in caplog.records if "after the step's bound" in r.getMessage()]
    assert bool(after_bound) is logged


async def test_a_cancelled_wrapper_left_behind_logs_nothing(monkeypatch, acme, caplog):
    monkeypatch.setattr(step_mod.refresh_owner, "is_live", lambda: True)
    started: list[asyncio.Task[Any]] = []

    async def resolve(cid, pid, ss, *, allow_refresh=True):
        if not allow_refresh:
            return None
        task = asyncio.current_task()
        assert task is not None
        started.append(task)
        await asyncio.Event().wait()

    monkeypatch.setattr(step_mod, "resolve_managed_auth", resolve)
    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.connectors.runtime.credentials_step"):
        await step_mod.bring_credentials_up_to_date([_entry()], timeout=0.05)
        started[0].cancel()
        for _ in range(5):
            await asyncio.sleep(0)
    assert not [r for r in caplog.records if "after the step's bound" in r.getMessage()]
