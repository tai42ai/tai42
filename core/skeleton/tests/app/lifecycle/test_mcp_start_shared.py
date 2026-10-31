"""One start per MCP server at a time in a serving generation: every door joins the start in flight,
a tool listing read launches no start of its own, and a start that ended leaves no record saying it is
still in progress."""

from __future__ import annotations

import functools
import logging

import pytest
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_contract.manifest.errors import McpListingUnavailableError

from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.manifest import Manifest

from ...connectors.credential_path import on_loop, owner_loop
from ._late_starts import _CountingListing, _rebuild_listing, _slow_mixin, _until


@pytest.mark.parametrize(
    "door",
    [
        pytest.param(lambda m: m._reload_failed_mcps, id="reload_failed_mcps"),
        pytest.param(lambda m: functools.partial(m._reload_mcp, "svc"), id="reload_mcp"),
    ],
)
def test_a_reload_door_joins_the_start_a_starting_record_waits_on_and_the_server_binds(
    short_reload_wait, monkeypatch, caplog, door
):
    # A second start of this server would fail while the first one runs.
    server = _CountingListing((0.6, None), (0.05, ConnectionError("another instance is starting")))
    m = _slow_mixin(monkeypatch, server)
    with caplog.at_level(logging.INFO), owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        assert m._failed_mcps["svc"]["category"] == "starting"
        on_loop(loop, reload_gate.run(door(m), reimports=False))
        # The door applied the shared start's outcome; the rebuild's late hook then finds nothing to apply.
        _until(lambda: any("probe outcome not applied" in r.getMessage() for r in caplog.records))
    assert (server.started, server.max_live) == (1, 1)
    assert "svc" not in m._failed_mcps
    assert m._mcp_bound_tools["svc"] == {"svc_t"}
    assert [r.getMessage() for r in caplog.records if "probe outcome not applied" in r.getMessage()] == [
        "MCP server 'svc': probe outcome not applied — the server is not recorded failed"
    ]


def test_a_read_during_a_rebuilds_start_answers_starting_and_a_retry_after_it_bound_lists(
    short_reload_wait, monkeypatch
):
    # Every start of this server takes 0.4s; the rebuild waits 0.1s (the reload wait) and records it starting.
    server = _CountingListing(*[(0.4, None)] * 4)
    m = _slow_mixin(monkeypatch, server)
    with owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        with pytest.raises(McpListingUnavailableError) as caught:
            on_loop(loop, m._advertised_mcp_tools("svc"))
        assert caught.value.cause == "starting"
        _until(lambda: "svc" not in m._failed_mcps)
        rows = on_loop(loop, m._advertised_mcp_tools("svc"))
    assert [row["name"] for row in rows] == ["svc_t"]
    assert server.started == 1


@pytest.mark.parametrize(
    ("manifest_after", "expected_message"),
    [
        pytest.param(
            {},
            "MCP server 'svc' start ended but its outcome was not applied: removed from the manifest while probed",
            id="title removed",
        ),
        pytest.param(
            {"mcp": [TaiMCPConfig(title="svc", config=MCPConfig(type="http", url="http://y/mcp")).model_dump()]},
            "MCP server 'svc' start ended but its outcome was not applied: its config was edited while probed; "
            "it is re-probed with its current config",
            id="config changed",
        ),
    ],
)
def test_a_start_whose_outcome_is_not_applied_leaves_a_record_that_says_it_ended(
    short_reload_wait, monkeypatch, manifest_after, expected_message
):
    server = _CountingListing((0.4, None))
    m = _slow_mixin(monkeypatch, server)
    with owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        m._manifest = Manifest.model_validate(manifest_after)
        _until(lambda: m._failed_mcps["svc"]["category"] != "starting")
    record = m._failed_mcps["svc"]
    assert (record["category"], record["message"]) == ("error", expected_message)
    assert m._start_awaited_by("svc") is None
    assert "svc" not in m._mcp_bound_tools


def test_a_start_whose_outcome_is_not_applied_for_an_edited_config_is_re_probed_with_that_config(
    short_reload_wait, monkeypatch
):
    server = _CountingListing((0.4, None), (0.0, None))
    edited = TaiMCPConfig(title="svc", config=MCPConfig(type="http", url="http://y/mcp"))
    m = _slow_mixin(monkeypatch, server)
    monkeypatch.setattr(m, "_refresh_manifest_mcp", lambda: None)
    with owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        m._manifest = Manifest.model_validate({"mcp": [edited.model_dump()]})
        _until(lambda: m._failed_mcps["svc"]["category"] != "starting")
        results = on_loop(loop, m._reprobe_failed_titles(["svc"]))
    assert [(r["title"], r["status"]) for r in results] == [("svc", "ok")]
    assert "svc" not in m._failed_mcps
    assert m._mcp_bound_tools["svc"] == {"svc_t"}


def test_a_late_outcome_for_a_record_that_waits_on_no_start_says_so(short_reload_wait, monkeypatch, caplog):
    # The title reload door joins the rebuild's start, waits the whole start budget and records its failure
    # first; the rebuild's late hook then finds a record that waits on no start.
    server = _CountingListing((0.4, ConnectionError("Connection closed")))
    m = _slow_mixin(monkeypatch, server)
    with caplog.at_level(logging.INFO), owner_loop() as loop:
        m._serving_loop = loop
        _rebuild_listing(m, loop)
        assert m._failed_mcps["svc"]["category"] == "starting"
        on_loop(loop, reload_gate.run(functools.partial(m._reload_mcp, "svc"), reimports=False))
        _until(lambda: any("not applied" in r.getMessage() for r in caplog.records))
    [skipped] = [r.getMessage() for r in caplog.records if "not applied" in r.getMessage()]
    assert skipped == "MCP server 'svc': probe outcome not applied — its failed record waits on no start"
    assert m._failed_mcps["svc"]["category"] == "unreachable"
    assert server.started == 1
