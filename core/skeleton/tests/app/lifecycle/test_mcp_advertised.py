"""The advertised-tools read: every tool a live MCP server lists, before the selection, with its bind state.

Drives the REAL app (``instance.app``) with a faked MCP listing, so the read and the binder
see the same server; the ``pg`` fixture is the stateful in-memory store.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import mcp
import pytest
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_contract.presets import PresetBody
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import NotFoundError, UpstreamError
from tai42_skeleton.operations.manifest import list_mcp_advertised_tools

from ...versioning.conftest import FakeVersioningPg

_OBJECT = {"type": "object", "properties": {}}


def _tool(name: str, schema: dict[str, Any] | None = None) -> mcp.types.Tool:
    return mcp.types.Tool(name=name, description=f"{name} things", inputSchema=schema or _OBJECT)


def _tools(*names: str) -> list[mcp.types.Tool]:
    return [_tool(name) for name in names]


def _cfg(title: str = "acme", include: list[str] | None = None, exclude: list[str] | None = None) -> TaiMCPConfig:
    return TaiMCPConfig(
        title=title,
        include=include or [],
        exclude=exclude or [],
        config=MCPConfig(type="http", url="http://x/mcp"),
    )


def _manifest(*entries: TaiMCPConfig) -> Manifest:
    return Manifest.model_validate(
        {
            "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "echo"]}],
            "mcp": [entry.model_dump() for entry in entries],
        }
    )


@pytest.fixture
def pg(monkeypatch) -> FakeVersioningPg:
    fake = FakeVersioningPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield fake

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    return fake


@pytest.fixture(autouse=True)
def _reset_preset_registry():
    yield
    mgr = instance.app.preset_manager

    async def _clear() -> None:
        for name in list(mgr.registered_names()):
            await mgr.remove(name)
        provider = instance.app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    for name in list(mgr.quarantined_names()):
        mgr.drop_quarantine(name)


def _listing(monkeypatch, tools: list[mcp.types.Tool]) -> AsyncMock:
    probe = AsyncMock(return_value=tools)
    monkeypatch.setattr(instance.app, "_probe_mcp", probe)
    return probe


async def _seed(name: str, base_tool: str) -> None:
    body = PresetBody(base_tool=base_tool, description="d", fixed_kwargs={}, extensions=[])
    await instance.app.versioning.store.create("preset", name, body.model_dump())


async def _reload(title: str) -> dict[str, Any]:
    return await reload_gate.run(lambda: instance.app.admin.reload_mcp(title), reimports=False)


async def _deregister(title: str) -> dict[str, Any]:
    return await reload_gate.run(lambda: instance.app.admin.deregister_mcp(title), reimports=False)


def _by_name(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in rows}


def _reasons(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    return {row["name"]: row["unbound_reason"] for row in rows}


def test_every_advertised_tool_is_listed_with_its_selection_and_binding(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send", "read", "delete"))
        async with instance.app.app_context(_manifest(_cfg(include=["acme_send"]))):
            rows = await instance.app.admin.advertised_mcp_tools("acme")

            assert [row["name"] for row in rows] == ["acme_delete", "acme_read", "acme_send"]
            assert {name: row["selected"] for name, row in _by_name(rows).items()} == {
                "acme_delete": False,
                "acme_read": False,
                "acme_send": True,
            }
            bound = instance.app.tools.mcp_bound_names("acme")
            assert {name: row["bound"] for name, row in _by_name(rows).items()} == {
                name: name in bound for name in ("acme_delete", "acme_read", "acme_send")
            }
            assert bound == {"acme_send"}
            assert _by_name(rows)["acme_send"]["description"] == "send things"
            assert _reasons(rows) == {"acme_delete": None, "acme_read": None, "acme_send": None}

    asyncio.run(run())


def test_an_unknown_title_raises_lookup_error_and_the_op_answers_404(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send"))
        async with instance.app.app_context(_manifest(_cfg())):
            with pytest.raises(LookupError, match="unknown mcp title 'nope'"):
                await instance.app.admin.advertised_mcp_tools("nope")
            with pytest.raises(NotFoundError, match="unknown mcp title 'nope'"):
                await list_mcp_advertised_tools("nope")

    asyncio.run(run())


def test_a_server_that_times_out_answers_502_with_the_redacted_cause(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send"))
        async with instance.app.app_context(_manifest(_cfg())):
            instance.app._probe_mcp = AsyncMock(  # type: ignore[method-assign]
                side_effect=TimeoutError("no answer from https://alice:hunter2@mcp.example/mcp?token=abc")
            )
            with pytest.raises(UpstreamError) as caught:
                await list_mcp_advertised_tools("acme")
        message = str(caught.value)
        assert message.startswith("mcp server 'acme' did not list its tools: no answer from ")
        assert "hunter2" not in message
        assert "token=abc" not in message
        assert caught.value.status == 502

    asyncio.run(run())


@pytest.mark.parametrize(
    ("entry", "unadvertised"),
    [
        (_cfg(include=["acme_send", "acme_gone"]), ["acme_gone"]),
        (_cfg(exclude=["acme_old"]), ["acme_old"]),
        (_cfg(), []),
    ],
)
def test_the_op_names_the_set_list_names_the_server_no_longer_advertises(monkeypatch, entry, unadvertised):
    async def run():
        _listing(monkeypatch, _tools("send", "read", "delete"))
        async with instance.app.app_context(_manifest(_cfg())):
            # The stored lists name what the server listed when they were set; the server
            # has since stopped advertising some of those names.
            live = instance.app._require_live_manifest()
            live.replace_mcp([entry])
            answer = await list_mcp_advertised_tools("acme")
        assert answer["title"] == "acme"
        assert answer["include"] == entry.include
        assert answer["exclude"] == entry.exclude
        assert [row["name"] for row in answer["tools"]] == ["acme_delete", "acme_read", "acme_send"]
        assert answer["unadvertised"] == unadvertised

    asyncio.run(run())


def test_an_include_name_the_server_does_not_list_binds_the_rest_and_reads_unadvertised(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send", "read", "delete"))
        async with instance.app.app_context(_manifest(_cfg(include=["acme_send", "acme_gone"]))):
            assert instance.app.tools.mcp_bound_names("acme") == {"acme_send"}
            assert (await list_mcp_advertised_tools("acme"))["unadvertised"] == ["acme_gone"]

            reloaded = await _reload("acme")
            assert (reloaded["status"], reloaded["tools"]) == ("ok", ["acme_send"])
            assert (await list_mcp_advertised_tools("acme"))["unadvertised"] == ["acme_gone"]

    asyncio.run(run())


def test_a_module_include_name_no_module_produces_still_fails_the_boot(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send"))
        manifest = Manifest.model_validate(
            {
                "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["echo", "fx_gone"]}],
                "mcp": [_cfg(include=["acme_send"]).model_dump()],
            }
        )
        with pytest.raises(TaiValidationError, match="Tools 'fx_gone' not found in server"):
            async with instance.app.app_context(manifest):
                pass

    asyncio.run(run())


def test_a_stored_preset_owning_a_selected_name_reads_preset_conflict(pg, monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send", "read", "delete"))
        await _seed("acme_send", "echo")
        async with instance.app.app_context(_manifest(_cfg(include=["acme_send", "acme_read"]))):
            rows = _by_name(await instance.app.admin.advertised_mcp_tools("acme"))
            assert rows["acme_delete"]["selected"] is False
            assert rows["acme_delete"]["unbound_reason"] is None
            assert rows["acme_send"]["selected"] is True
            assert rows["acme_send"]["bound"] is False
            assert rows["acme_send"]["unbound_reason"] == "preset_conflict"
            assert rows["acme_read"]["unbound_reason"] is None

    asyncio.run(run())


def test_an_unusable_schema_reads_unusable_schema_and_is_not_served(monkeypatch):
    async def run():
        tools = [_tool("send"), _tool("read", {"type": "object", "properties": {"q": {"anyOf": []}}}), _tool("delete")]
        _listing(monkeypatch, tools)
        async with instance.app.app_context(_manifest(_cfg())):
            rows = await instance.app.admin.advertised_mcp_tools("acme")
            assert [row["name"] for row in rows] == ["acme_delete", "acme_read", "acme_send"]
            assert _reasons(rows) == {"acme_delete": None, "acme_read": "unusable_schema", "acme_send": None}
            served = await instance.app.admin.served_mcp_names(_cfg())
            assert sorted(served) == ["acme_delete", "acme_send"]

    asyncio.run(run())


def test_a_tool_listed_after_the_last_bind_reads_not_listed_at_bind(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send", "read"))
        async with instance.app.app_context(_manifest(_cfg())):
            instance.app._probe_mcp = AsyncMock(return_value=_tools("send", "read", "archive"))  # type: ignore[method-assign]
            rows = _by_name(await instance.app.admin.advertised_mcp_tools("acme"))
            assert rows["acme_archive"]["selected"] is True
            assert rows["acme_archive"]["bound"] is False
            assert rows["acme_archive"]["unbound_reason"] == "not_listed_at_bind"

    asyncio.run(run())


def test_a_server_whose_last_bind_failed_reads_server_failed_over_the_other_records(pg, monkeypatch):
    async def run():
        tools = [_tool("send"), _tool("read", {"type": "object", "properties": {"q": {"anyOf": []}}}), _tool("delete")]
        _listing(monkeypatch, tools)
        await _seed("acme_send", "echo")
        async with instance.app.app_context(_manifest(_cfg())):
            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme")) == {
                "acme_delete": None,
                "acme_read": "unusable_schema",
                "acme_send": "preset_conflict",
            }
            instance.app._probe_mcp = AsyncMock(side_effect=TimeoutError("down"))  # type: ignore[method-assign]
            assert (await _reload("acme"))["status"] == "unavailable"
            assert "acme" in instance.app._failed_mcps
            instance.app._probe_mcp = AsyncMock(return_value=tools)  # type: ignore[method-assign]

            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme")) == {
                "acme_delete": None,
                "acme_read": "server_failed",
                "acme_send": "server_failed",
            }

    asyncio.run(run())


def test_a_server_failing_at_boot_reads_server_failed_on_every_selected_row(monkeypatch):
    async def run():
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(side_effect=TimeoutError("down")))
        async with instance.app.app_context(_manifest(_cfg())):
            instance.app._probe_mcp = AsyncMock(return_value=_tools("send", "read"))  # type: ignore[method-assign]
            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme")) == {
                "acme_read": "server_failed",
                "acme_send": "server_failed",
            }

    asyncio.run(run())


def test_a_deregistered_server_reads_not_loaded_until_a_reload_binds_it_again(pg, monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send", "read"))
        await _seed("acme_send", "echo")
        async with instance.app.app_context(_manifest(_cfg())):
            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme"))["acme_send"] == "preset_conflict"
            await _deregister("acme")
            assert "acme" in {entry.title for entry in instance.app._require_live_manifest().mcp}

            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme")) == {
                "acme_read": "not_loaded",
                "acme_send": "not_loaded",
            }

            await _reload("acme")
            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme")) == {
                "acme_read": None,
                "acme_send": "preset_conflict",
            }

    asyncio.run(run())


def test_an_entry_this_worker_has_not_bound_reads_not_loaded(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send"))
        async with instance.app.app_context(_manifest(_cfg())):
            live = instance.app._require_live_manifest()
            live.replace_mcp([*live.mcp, _cfg("beta")])
            instance.app._probe_mcp = AsyncMock(return_value=_tools("send"))  # type: ignore[method-assign]
            assert _reasons(await instance.app.admin.advertised_mcp_tools("beta")) == {"beta_send": "not_loaded"}

    asyncio.run(run())
