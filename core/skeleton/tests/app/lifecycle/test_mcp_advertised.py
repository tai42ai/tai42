"""The advertised-tools read: every tool an MCP server listed at its bind on this worker, before the
selection, with its bind state; with no listing held, the server's failed record or its absence answers.

Drives the REAL app (``instance.app``) with a faked MCP listing, so the bind lists the faked tools;
the ``pg`` fixture is the stateful in-memory store.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import mcp
import pytest
from tai42_contract.errors import UnavailableError
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_contract.presets import PresetBody
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.settings.mcp_settings as mcp_settings_module
import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.app.epoch import Epoch, building
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import NotFoundError, UpstreamError
from tai42_skeleton.operations.manifest import list_mcp_advertised_tools
from tai42_skeleton.tools.binding.surface import remove_surface_tool
from tai42_skeleton.tools.mcp_start import McpStartPending, StartId

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
            remove_surface_tool(instance.app._fast_mcp, tool.name)

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


def test_a_read_during_a_rebuild_answers_the_live_generations_rows(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send", "read"))
        async with instance.app.app_context(_manifest(_cfg(include=["acme_send"]))):
            # A rebuild's generation under construction: its own build has not listed yet. Even the
            # build's own work reads the live generation's advertised rows.
            with building(Epoch(number=0, core=instance.app._build_serving_core())):
                rows = await instance.app.admin.advertised_mcp_tools("acme")
                served = await instance.app.admin.served_mcp_names(_cfg(include=["acme_send"]))

            assert [row["name"] for row in rows] == ["acme_read", "acme_send"]
            assert served == ["acme_send", "acme_read"]

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


def test_a_server_failing_at_boot_answers_502_with_its_recorded_cause_and_launches_no_start(monkeypatch):
    async def run():
        probe = AsyncMock(side_effect=TimeoutError("no answer from https://alice:hunter2@mcp.example/mcp?token=abc"))
        monkeypatch.setattr(instance.app, "_probe_mcp", probe)
        async with instance.app.app_context(_manifest(_cfg())):
            starts = probe.await_count
            recorded = instance.app._failed_mcps["acme"]["message"]
            with pytest.raises(UpstreamError) as caught:
                await list_mcp_advertised_tools("acme")
            assert probe.await_count == starts
        message = str(caught.value)
        assert message == f"mcp server 'acme' did not list its tools: {recorded}"
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


def test_a_bound_server_whose_reload_failed_reads_the_rows_of_its_last_bind_and_launches_no_start(pg, monkeypatch):
    async def run():
        tools = [_tool("send"), _tool("read", {"type": "object", "properties": {"q": {"anyOf": []}}}), _tool("delete")]
        _listing(monkeypatch, tools)
        await _seed("acme_send", "echo")
        async with instance.app.app_context(_manifest(_cfg())):
            failing = AsyncMock(side_effect=TimeoutError("down"))
            instance.app._probe_mcp = failing  # type: ignore[method-assign]
            assert (await _reload("acme"))["status"] == "unavailable"
            assert "acme" in instance.app._failed_mcps
            assert instance.app.tools.mcp_bound_names("acme") == {"acme_delete"}
            starts = failing.await_count

            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme")) == {
                "acme_delete": None,
                "acme_read": "unusable_schema",
                "acme_send": "preset_conflict",
            }
            assert failing.await_count == starts

    asyncio.run(run())


def test_a_server_whose_start_is_in_progress_answers_503_with_the_starting_code(monkeypatch):
    async def run():
        pending = McpStartPending("acme", 3.0, 15.0, start=StartId())
        probe = AsyncMock(side_effect=pending)
        monkeypatch.setattr(instance.app, "_probe_mcp", probe)
        async with instance.app.app_context(_manifest(_cfg())):
            assert instance.app._failed_mcps["acme"]["category"] == "starting"
            starts = probe.await_count
            with pytest.raises(UnavailableError) as caught:
                await list_mcp_advertised_tools("acme")
            assert probe.await_count == starts
        assert str(caught.value) == (
            "mcp server 'acme' did not list its tools: "
            "MCP server 'acme' start still in progress after 3.0s; it continues up to the start budget 15.0s; "
            "it binds once its start lists its tools"
        )
        assert caught.value.status == 503
        assert caught.value.extra == {"code": "mcp-server-starting"}

    asyncio.run(run())


_NOT_LOADED = "MCP server {title!r} is not loaded on this worker; a reload binds it"


async def _not_loaded(title: str) -> UnavailableError:
    with pytest.raises(UnavailableError) as caught:
        await list_mcp_advertised_tools(title)
    assert str(caught.value) == f"mcp server {title!r} did not list its tools: " + _NOT_LOADED.format(title=title)
    assert caught.value.status == 503
    assert caught.value.extra == {"code": "mcp-server-not-loaded"}
    return caught.value


def test_a_deregistered_server_answers_503_not_loaded_until_a_reload_binds_it_again(pg, monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send", "read"))
        await _seed("acme_send", "echo")
        async with instance.app.app_context(_manifest(_cfg())):
            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme"))["acme_send"] == "preset_conflict"
            await _deregister("acme")
            assert "acme" in {entry.title for entry in instance.app._require_live_manifest().mcp}

            await _not_loaded("acme")

            await _reload("acme")
            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme")) == {
                "acme_read": None,
                "acme_send": "preset_conflict",
            }

    asyncio.run(run())


def test_an_entry_this_worker_has_not_bound_answers_503_not_loaded_until_a_reload_binds_it(monkeypatch):
    async def run():
        probe = _listing(monkeypatch, _tools("send"))
        async with instance.app.app_context(_manifest(_cfg())):
            live = instance.app._require_live_manifest()
            live.replace_mcp([*live.mcp, _cfg("beta")])
            starts = probe.await_count

            await _not_loaded("beta")
            assert probe.await_count == starts

            await _reload("beta")
            assert _reasons(await instance.app.admin.advertised_mcp_tools("beta")) == {"beta_send": None}

    asyncio.run(run())


def test_a_selection_grafted_after_the_bind_reads_pending_bind_until_a_reload_binds_it(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send", "read"))
        async with instance.app.app_context(_manifest(_cfg(include=["acme_send"]), _cfg("beta"))):
            # The stored rows now select ``acme_read`` too; the reload of another title grafts
            # them into the live manifest without rebinding ``acme``.
            stored = _manifest(_cfg(include=["acme_send", "acme_read"]), _cfg("beta"))
            monkeypatch.setattr(instance.app.config.config_manager, "read_manifest", lambda: stored.model_dump())
            await _reload("beta")

            rows = _by_name(await instance.app.admin.advertised_mcp_tools("acme"))
            assert {name: (row["selected"], row["bound"]) for name, row in rows.items()} == {
                "acme_read": (True, False),
                "acme_send": (True, True),
            }
            assert _reasons(list(rows.values())) == {"acme_read": "pending_bind", "acme_send": None}

            await _reload("acme")
            rows = _by_name(await instance.app.admin.advertised_mcp_tools("acme"))
            assert {name: row["bound"] for name, row in rows.items()} == {"acme_read": True, "acme_send": True}
            assert _reasons(list(rows.values())) == {"acme_read": None, "acme_send": None}

    asyncio.run(run())


def test_a_re_fired_start_whose_probe_fails_drops_every_bind_record_of_the_last_start(pg, monkeypatch):
    async def run():
        tools = [_tool("send"), _tool("read", {"type": "object", "properties": {"q": {"anyOf": []}}}), _tool("delete")]
        probe = _listing(monkeypatch, tools)
        await _seed("acme_send", "echo")
        manifest = _manifest(_cfg())
        async with instance.app.app_context(manifest):
            assert _reasons(await instance.app.admin.advertised_mcp_tools("acme")) == {
                "acme_delete": None,
                "acme_read": "unusable_schema",
                "acme_send": "preset_conflict",
            }

            probe.side_effect = TimeoutError("no answer")
            await instance.app.start(manifest)

            core = instance.app._live_serving_core
            assert "acme" not in core._mcp_listed_tools
            assert "acme" not in core._mcp_preset_conflicts
            assert "acme" not in core._mcp_unusable_tools
            with pytest.raises(UpstreamError) as caught:
                await list_mcp_advertised_tools("acme")
            assert caught.value.status == 502

    asyncio.run(run())


def test_a_rebind_that_fails_after_its_probe_holds_no_listing_and_reads_502_with_the_recorded_cause(monkeypatch):
    async def run():
        probe = AsyncMock(side_effect=TimeoutError("down"))
        monkeypatch.setattr(instance.app, "_probe_mcp", probe)
        async with instance.app.app_context(_manifest(_cfg())):
            probe.side_effect = None
            probe.return_value = _tools("send", "read")

            def malformed_settings():
                raise ValueError("malformed TAI_MCP_SCHEMA_MAX_DEPTH")

            monkeypatch.setattr(mcp_settings_module, "mcp_dispatch_settings", malformed_settings)
            results = await reload_gate.run(instance.app.admin.reload_failed_mcps, reimports=False)
            assert results == [{"title": "acme", "status": "error"}]
            record = instance.app._failed_mcps["acme"]
            assert record["category"] == "error"

            assert "acme" not in instance.app._live_serving_core._mcp_listed_tools
            with pytest.raises(UpstreamError) as caught:
                await list_mcp_advertised_tools("acme")
            assert caught.value.status == 502
            assert str(caught.value) == f"mcp server 'acme' did not list its tools: {record['message']}"

    asyncio.run(run())


def test_a_single_reload_whose_bind_fails_after_its_probe_records_the_failure_and_reads_502(monkeypatch):
    async def run():
        _listing(monkeypatch, _tools("send", "read"))
        async with instance.app.app_context(_manifest(_cfg())):
            assert instance.app.tools.mcp_bound_names("acme") == {"acme_send", "acme_read"}

            def malformed_settings():
                raise ValueError("malformed TAI_MCP_SCHEMA_MAX_DEPTH")

            monkeypatch.setattr(mcp_settings_module, "mcp_dispatch_settings", malformed_settings)
            with pytest.raises(ValueError, match="malformed TAI_MCP_SCHEMA_MAX_DEPTH"):
                await _reload("acme")
            record = instance.app._failed_mcps["acme"]
            assert record["category"] == "error"
            assert "malformed TAI_MCP_SCHEMA_MAX_DEPTH" in record["message"]

            assert "acme" not in instance.app._live_serving_core._mcp_listed_tools
            with pytest.raises(UpstreamError) as caught:
                await list_mcp_advertised_tools("acme")
            assert caught.value.status == 502
            assert str(caught.value) == f"mcp server 'acme' did not list its tools: {record['message']}"

    asyncio.run(run())
