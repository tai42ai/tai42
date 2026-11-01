"""An MCP server start that outlives its caller's wait: the ``starting`` record, the late
outcome's apply on the serving loop under the reload gate, and the context it runs from."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging

import pytest
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_contract.manifest.errors import McpListingUnavailableError

from tai42_skeleton.app.lifecycle import mcp_probe as mcp_probe_module
from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.mcp_start import McpStartPending, StartId

from ...connectors.credential_path import on_loop, owner_loop
from ._doubles import _cfg, _failed_row, _mixin
from ._late_starts import (
    _CountingListing,
    _next_generation,
    _rebuild_listing,
    _SequencedListing,
    _slow_mixin,
    _SlowListing,
    _until,
)

# -- a start the rebuild stops waiting for is recorded ``starting`` -----------------------------


def test_a_pending_start_is_categorised_starting():
    assert (
        mcp_probe_module._mcp_failure_category(McpStartPending("svc", 3.0, 15.0, start=StartId()), None) == "starting"
    )


def test_a_start_past_the_reload_wait_is_recorded_starting_and_not_cancelled(short_reload_wait, monkeypatch):
    server = _SlowListing(0.5)
    m = _slow_mixin(monkeypatch, server)
    with owner_loop() as loop:
        m._serving_loop = loop
        assert _rebuild_listing(m, loop) == []
        record = m._failed_mcps["svc"]
        assert (record["status"], record["category"], record["http_status"]) == ("unavailable", "starting", None)
        assert record["message"] == (
            "MCP server 'svc' start still in progress after 0.1s; "
            "it continues up to the start budget 10.0s; it binds once its start lists its tools"
        )
        _until(lambda: "svc" not in m._failed_mcps)
        assert not server.cancelled.is_set()


def test_a_starting_record_logs_at_info_that_the_server_binds_once_its_start_lists_its_tools(
    short_reload_wait, monkeypatch, caplog
):
    server = _SlowListing(0.3)
    m = _slow_mixin(monkeypatch, server)
    with owner_loop() as loop, caplog.at_level(logging.INFO, logger=mcp_probe_module.__name__):
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        _until(lambda: "svc" not in m._failed_mcps)
    [record] = [r for r in caplog.records if "start still in progress" in r.getMessage()]
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        "MCP server 'svc' still starting, recorded starting until its start completes: "
        "MCP server 'svc' start still in progress after 0.1s; "
        "it continues up to the start budget 10.0s; it binds once its start lists its tools"
    )


def test_a_start_that_completes_after_the_reload_wait_binds_through_the_apply_seam(short_reload_wait, monkeypatch):
    server = _SlowListing(0.4)
    m = _slow_mixin(monkeypatch, server)
    applied: list[str] = []
    apply = m._apply_reloaded_mcp

    async def recording_apply(title, config, tools):
        applied.append(title)
        return await apply(title, config, tools)

    monkeypatch.setattr(m, "_apply_reloaded_mcp", recording_apply)
    with owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        assert m._failed_mcps["svc"]["category"] == "starting"
        _until(lambda: "svc" not in m._failed_mcps)
    assert applied == ["svc"]
    assert m._mcp_bound_tools["svc"] == {"svc_t"}


def test_a_late_failure_is_recorded_through_the_failed_record(short_reload_wait, monkeypatch):
    server = _SlowListing(0.4, error=ConnectionError("Connection closed"))
    m = _slow_mixin(monkeypatch, server)
    with owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        assert m._failed_mcps["svc"]["category"] == "starting"
        _until(lambda: m._failed_mcps["svc"]["category"] != "starting")
    assert m._failed_mcps["svc"] == _failed_row(category="unreachable", message="Connection closed")
    assert "svc" not in m._mcp_bound_tools


@pytest.mark.parametrize(
    ("manifest_after", "expected_log"),
    [
        pytest.param(
            {},
            "MCP server 'svc': probe outcome not applied — removed from the manifest while probed",
            id="title removed",
        ),
        pytest.param(
            {"mcp": [TaiMCPConfig(title="svc", config=MCPConfig(type="http", url="http://y/mcp")).model_dump()]},
            "MCP server 'svc': probe outcome not applied — its config was edited while probed",
            id="config changed",
        ),
    ],
)
def test_a_late_outcome_for_a_title_no_longer_as_probed_is_not_applied(
    short_reload_wait, monkeypatch, caplog, manifest_after, expected_log
):
    server = _SlowListing(0.4)
    m = _slow_mixin(monkeypatch, server)
    with caplog.at_level(logging.INFO), owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        m._manifest = Manifest.model_validate(manifest_after)
        _until(lambda: any("probe outcome not applied" in r.getMessage() for r in caplog.records))
    [skipped] = [r.getMessage() for r in caplog.records if "probe outcome not applied" in r.getMessage()]
    assert skipped == expected_log
    assert m._failed_mcps["svc"]["category"] == "error"
    assert "svc" not in m._mcp_bound_tools


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        pytest.param(_SlowListing(0.4), {"successes": ["svc"], "failed": {}}, id="lists"),
        pytest.param(
            _SlowListing(0.4, error=ConnectionError("Connection closed")),
            {"successes": [], "failed": {"svc": _failed_row(category="unreachable", message="Connection closed")}},
            id="fails",
        ),
    ],
)
def test_with_no_serving_loop_a_rebuild_waits_the_start_budget_and_leaves_no_starting_record(
    short_reload_wait, monkeypatch, server, expected
):
    # A late outcome has no serving loop to be applied on, so the start is waited for whole.
    m = _slow_mixin(monkeypatch, server)
    assert m._serving_loop is None
    successes, failures = run_blocking(m._load_mcps)
    for config, exc in failures:
        m._record_failed_mcp(config, exc)
    assert {"successes": [config.title for config, _tools in successes], "failed": m._failed_mcps} == expected
    assert not server.cancelled.is_set()


def test_a_late_outcome_after_the_serving_loop_stopped_is_logged_at_error_naming_the_title(
    short_reload_wait, monkeypatch, caplog
):
    server = _SlowListing(0.6)
    m = _slow_mixin(monkeypatch, server)
    with caplog.at_level(logging.ERROR):
        with owner_loop() as loop:
            m._serving_loop = loop
            _rebuild_listing(m, loop)
            assert m._failed_mcps["svc"]["category"] == "starting"
        assert server.listed.wait(5)
        _until(lambda: any(r.levelno == logging.ERROR and "'svc'" in r.getMessage() for r in caplog.records))
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR and "'svc'" in r.getMessage()]
    assert record.getMessage() == (
        "MCP server 'svc' finished starting after its caller stopped waiting, but no serving loop "
        "is running to apply the outcome (listed)"
    )
    assert "svc" not in m._mcp_bound_tools


_CALLER_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar("caller_scope", default="<none>")


def test_a_late_outcome_is_applied_on_the_serving_loop_from_no_callers_context(short_reload_wait, monkeypatch):
    server = _SlowListing(0.3)
    m = _slow_mixin(monkeypatch, server)
    seen: list[tuple[str, asyncio.AbstractEventLoop]] = []

    async def recording_apply(title, config, outcome, *, awaited):
        seen.append((_CALLER_SCOPE.get(), asyncio.get_running_loop()))

    monkeypatch.setattr(m, "_apply_probe_outcome", recording_apply)

    async def request() -> None:
        _CALLER_SCOPE.set("request-42 caller identity")
        with pytest.raises(McpStartPending) as pending:
            await m._probe_mcp(_cfg("svc"), wait=0.05)
        m._record_failed_mcp(_cfg("svc"), pending.value)

    with owner_loop() as loop:
        m._serving_loop = loop
        asyncio.run(request())
        _until(lambda: seen != [])
    assert seen == [("<none>", loop)]


def _reload_title(m, loop, title: str = "svc") -> None:
    on_loop(loop, reload_gate.run(functools.partial(m._reload_mcp, title), reimports=False))


def test_a_bound_servers_read_answers_its_retained_listing_and_launches_no_start(short_reload_wait, monkeypatch):
    server = _CountingListing((0.0, None))
    m = _slow_mixin(monkeypatch, server)
    with owner_loop() as loop:
        m._serving_loop = loop
        _reload_title(m, loop)
        assert server.started == 1
        rows = on_loop(loop, m._advertised_mcp_tools("svc"))
    assert [row["name"] for row in rows] == ["svc_t"]
    assert server.started == 1


def test_a_late_start_that_binds_retains_its_listing_for_the_read(short_reload_wait, monkeypatch):
    server = _CountingListing((0.4, None))
    m = _slow_mixin(monkeypatch, server)
    with owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        assert m._failed_mcps["svc"]["category"] == "starting"
        _until(lambda: "svc" not in m._failed_mcps)
        rows = on_loop(loop, m._advertised_mcp_tools("svc"))
    assert [row["name"] for row in rows] == ["svc_t"]
    assert server.started == 1


def test_a_new_generation_holds_no_listing_until_its_own_build_lists(short_reload_wait, monkeypatch):
    server = _CountingListing((0.0, None), (0.4, None))
    m = _slow_mixin(monkeypatch, server)
    with owner_loop() as loop:
        m._serving_loop = loop
        _reload_title(m, loop)
        _next_generation(m)
        with pytest.raises(McpListingUnavailableError) as caught:
            on_loop(loop, m._advertised_mcp_tools("svc"))
        assert caught.value.cause == "not_loaded"
        _rebuild_listing(m, loop)
        _until(lambda: "svc" not in m._failed_mcps)
        rows = on_loop(loop, m._advertised_mcp_tools("svc"))
    assert [row["name"] for row in rows] == ["svc_t"]
    assert server.started == 2


def test_an_older_generations_late_outcome_leaves_the_record_of_the_newer_generations_start_alone(
    short_reload_wait, monkeypatch, caplog
):
    # The first rebuild's start fails late, while the next generation's own start is still running.
    server = _SequencedListing((0.3, ConnectionError("Connection closed")), (0.8, None))
    m = _slow_mixin(monkeypatch, server)
    with caplog.at_level(logging.INFO), owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        _next_generation(m)
        _rebuild_listing(m, loop)
        _until(lambda: any("not applied" in r.getMessage() for r in caplog.records))
        assert m._failed_mcps["svc"]["category"] == "starting"
        [skipped] = [r.getMessage() for r in caplog.records if "not applied" in r.getMessage()]
        assert skipped == "MCP server 'svc': probe outcome not applied — its failed record waits on another start"
        _until(lambda: "svc" not in m._failed_mcps)
    assert m._mcp_bound_tools["svc"] == {"svc_t"}


def test_a_late_start_whose_bind_fails_records_the_bind_failure(short_reload_wait, monkeypatch):
    server = _SlowListing(0.3)
    m = _slow_mixin(monkeypatch, server)

    async def failing_apply(title, config, tools):
        raise RuntimeError("binding broke at https://alice:hunter2@mcp.example/mcp")

    monkeypatch.setattr(m, "_apply_reloaded_mcp", failing_apply)
    with owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        assert m._failed_mcps["svc"]["category"] == "starting"
        _until(lambda: m._failed_mcps["svc"]["category"] != "starting")
    assert m._failed_mcps["svc"] == _failed_row(
        category="error", message="binding broke at https://***@mcp.example/mcp"
    )


def test_an_older_reprobe_failure_leaves_the_newer_starts_record_and_that_start_binds(
    short_reload_wait, monkeypatch, caplog
):
    # A re-probe pass's start fails after a rebuild's next generation recorded its own, newer, start as starting.
    server = _SequencedListing((0.6, ConnectionError("old start failed")), (0.9, None))
    m = _slow_mixin(monkeypatch, server)
    m._failed_mcps["svc"] = _failed_row(category="unreachable", message="down")
    with caplog.at_level(logging.INFO), owner_loop() as loop:
        m._serving_loop = loop
        reprobe = asyncio.run_coroutine_threadsafe(m._probe_and_apply_failed({"svc": _cfg("svc")}), loop)
        _until(lambda: len(server.listings) == 1)
        _next_generation(m)
        _rebuild_listing(m, loop)
        assert m._failed_mcps["svc"]["category"] == "starting"
        reprobe.result(5)
        assert m._failed_mcps["svc"]["category"] == "starting"
        _until(lambda: "svc" not in m._failed_mcps)
    assert m._mcp_bound_tools["svc"] == {"svc_t"}
    assert [r.getMessage() for r in caplog.records if "not applied" in r.getMessage()] == [
        "MCP server 'svc': probe outcome not applied — its failed record waits on another start"
    ]


def test_a_late_starts_credential_demand_is_noted_on_the_core_that_launched_it(short_reload_wait, monkeypatch):
    async def demanding_listing(config, listing, list_tools):
        await asyncio.sleep(0.3)
        listing.credential_demanded = True
        raise ConnectionError("Connection closed")

    monkeypatch.setattr(mcp_probe_module, "list_with_managed_auth", demanding_listing)
    m = _mixin(_cfg("svc"))
    launching = m._serving_core
    with owner_loop() as loop:
        m._serving_loop = loop
        with pytest.raises(McpStartPending):
            on_loop(loop, m._probe_mcp(_cfg("svc"), wait=0.05))
        m._building = next_core = m._build_serving_core()
        _until(lambda: "svc" in launching._mcp_credential_demanded | next_core._mcp_credential_demanded)
    assert launching._mcp_credential_demanded == {"svc"}
    assert next_core._mcp_credential_demanded == set()
