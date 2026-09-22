"""The MCP tool-call edge: secret reveal, run-row/trace recording, and the
offloaded-sync-tool case."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from tai42_contract.access_control import reset_request_user_id, set_request_user_id
from tai42_contract.interactions import SuspendedInteraction, read_suspended_interaction_marker
from tai42_contract.secrets import SECRET_PLACEHOLDER, SecretValue

from tai42_skeleton.app.instance import app
from tai42_skeleton.exceptions.exceptions import TurnTimeoutError
from tai42_skeleton.manifest import Manifest

from .conftest import _budget_flag, _LogCapture, _plain_tools_manifest

# -- the MCP tool-call edge secret reveal -------------------------------------


def test_mcp_edge_reveals_wrapped_secret_to_the_client():
    # The MCP ``tools/call`` edge is a live-caller door: a wrapped secret in the tool's
    # return is revealed before FastMCP serializes the result, so an in-memory MCP client
    # receives the real value in both structured and text content — never the placeholder.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def vault() -> dict:
                """Return a dict carrying a wrapped secret."""
                return {"token": SecretValue("tok-4242-xyzzy")}

            async with Client(app._fast_mcp) as client:
                result = await client.call_tool("vault", {})

            assert result.structured_content == {"token": "tok-4242-xyzzy"}
            texts = [block.text for block in result.content if isinstance(block, TextContent)]
            assert any("tok-4242-xyzzy" in text for text in texts)

    asyncio.run(run())


def test_mcp_edge_reveals_wrapped_secret_of_a_preset_to_the_client():
    # A PRESET reached over the MCP edge goes FastMCP -> TransformedTool.run directly
    # (the in-process reveal gate is unarmed), so the parent tool's convert_result
    # reveals: the live MCP caller of a preset receives the real value, exactly as a
    # direct tool call does.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def vault(account: str) -> dict:
                """Return an account's stored token wrapped as a secret."""
                return {"account": account, "token": SecretValue(f"tok-{account}")}

            await app.preset_manager.register("acme_vault", "vault", {"account": "acme"}, [], "Acme vault")
            try:
                async with Client(app._fast_mcp) as client:
                    result = await client.call_tool("acme_vault", {})
            finally:
                await app.preset_manager.remove("acme_vault")

            assert result.structured_content == {"account": "acme", "token": "tok-acme"}

    asyncio.run(run())


def test_mcp_call_of_a_registered_preset_registers_a_run_row_and_trace_root(monkeypatch):
    # An MCP ``tools/call`` of a registered preset registers exactly one run row carrying
    # the opened trace root's id — the same runs-index deep link the in-process door
    # produces — because the edge enters the shared ``dispatch_scope`` rather than
    # dispatching straight to ``Tool.run`` past the run-index chokepoint.
    import tai42_skeleton.runs.chokepoint as chokepoint
    from tai42_skeleton.monitoring import init_monitoring, reset_monitoring

    from .._fakes.recording_monitoring import RecordingMonitoring

    starts: list[dict[str, Any]] = []
    terminals: list[dict[str, Any]] = []

    class _SpyStore:
        async def insert_start(
            self, run_id, preset_name, preset_version, *, trace_id, user_id, session_id, interaction_id, started_at
        ):
            starts.append({"preset_name": preset_name, "trace_id": trace_id})

        async def update_outcome(
            self, run_id, outcome, ended_at, *, trace_id=None, interaction_id=None, resumed_interactions=None
        ):
            terminals.append({"outcome": outcome})

    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: _SpyStore())

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def echo(value: str) -> dict:
                """Echo the value back."""
                return {"value": value}

            await app.preset_manager.register("echo_preset", "echo", {"value": "x"}, [], "Echo preset")
            init_monitoring(RecordingMonitoring())
            try:
                async with Client(app._fast_mcp) as client:
                    result = await client.call_tool("echo_preset", {})
            finally:
                reset_monitoring()
                await app.preset_manager.remove("echo_preset")
            assert result.structured_content == {"value": "x"}

    asyncio.run(run())
    assert [s["preset_name"] for s in starts] == ["echo_preset"]
    assert starts[0]["trace_id"] == "trace-root"
    assert [t["outcome"] for t in terminals] == ["success"]


def test_mcp_call_of_a_parking_preset_records_parked_not_success(monkeypatch):
    # A registered preset whose dispatch async-parks (its body returns a
    # ``SuspendedInteraction``) records the ``parked`` terminal — carrying the park's
    # interaction id — when invoked over the MCP ``tools/call`` edge, the same outcome
    # the in-process door records. The edge reads the park off the reserved marker the
    # serialized tool result carries; a park whose sentinel fields were flattened onto
    # the wire would default to ``success``, silently losing the park.
    import tai42_skeleton.runs.chokepoint as chokepoint

    terminals: list[dict[str, Any]] = []

    class _SpyStore:
        async def insert_start(
            self, run_id, preset_name, preset_version, *, trace_id, user_id, session_id, interaction_id, started_at
        ):
            pass

        async def update_outcome(
            self, run_id, outcome, ended_at, *, trace_id=None, interaction_id=None, resumed_interactions=None
        ):
            terminals.append({"outcome": outcome, "interaction_id": interaction_id})

    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: _SpyStore())

    expiry = datetime(2030, 1, 1, tzinfo=UTC)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            # No declared output_schema: FastMCP would otherwise flatten the sentinel's
            # fields into structured_content, dropping the reserved marker key the edge
            # reads a park by. The edge must recognize the park regardless.
            @app.tools.tool(force=True)
            async def parker():
                """Park the caller and return the suspend sentinel."""
                return SuspendedInteraction(interaction_id="park-42", expiry_at=expiry, resume_owner="resume_tool")

            await app.preset_manager.register("parker_preset", "parker", {}, [], "Parker preset")
            try:
                async with Client(app._fast_mcp) as client:
                    result = await client.call_tool("parker_preset", {})
            finally:
                await app.preset_manager.remove("parker_preset")
            # The park rides the wire as the reserved marker, not flattened sentinel fields.
            marker = read_suspended_interaction_marker(result.structured_content)
            assert marker is not None
            assert marker["interaction_id"] == "park-42"

    asyncio.run(run())
    assert [t["outcome"] for t in terminals] == ["parked"]
    assert terminals[0]["interaction_id"] == "park-42"


def test_mcp_call_run_row_is_born_with_the_callers_user_id(monkeypatch):
    # An MCP-edge run row is born carrying the door's caller user_id (never NULL): the
    # edge deposits the run attribution from ``request_identity()`` before the scope, so
    # the START row records the caller rather than an unattributed row.
    import tai42_skeleton.runs.chokepoint as chokepoint

    starts: list[dict[str, Any]] = []

    class _SpyStore:
        async def insert_start(
            self, run_id, preset_name, preset_version, *, trace_id, user_id, session_id, interaction_id, started_at
        ):
            starts.append({"preset_name": preset_name, "user_id": user_id})

        async def update_outcome(
            self, run_id, outcome, ended_at, *, trace_id=None, interaction_id=None, resumed_interactions=None
        ):
            pass

    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: _SpyStore())

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def echo(value: str) -> dict:
                """Echo the value back."""
                return {"value": value}

            await app.preset_manager.register("echo_preset", "echo", {"value": "x"}, [], "Echo preset")
            token = set_request_user_id("caller-9")
            try:
                async with Client(app._fast_mcp) as client:
                    await client.call_tool("echo_preset", {})
            finally:
                reset_request_user_id(token)
                await app.preset_manager.remove("echo_preset")

    asyncio.run(run())
    assert [s["user_id"] for s in starts] == ["caller-9"]


def test_mcp_edge_secret_preset_schema_violation_redacts_the_secret_from_logs_and_client():
    # A PRESET over a secret-returning tool, reached over the live MCP edge with an
    # output_schema the REAL token VIOLATES (``minLength: 6`` vs the 5-char ``tok-x``,
    # which the 8-char ``[secret]`` placeholder would falsely satisfy): the reveal gate
    # is UNARMED, so convert_result reveals the secret into structured_content and the
    # output-schema guard validates the revealed value. The violation raises loudly, but
    # the plaintext token must ride NEITHER fastmcp's ``logger.exception`` NOR the error
    # returned to the client — the redacted failure keeps only the json_path and the
    # placeholder.
    schema = {
        "type": "object",
        "properties": {"account": {"type": "string"}, "token": {"type": "string", "minLength": 6}},
        "required": ["account", "token"],
    }
    capture = _LogCapture()
    server_logger = logging.getLogger("fastmcp.server.server")

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def vault(account: str) -> dict:
                """Return an account's stored token wrapped as a secret."""
                return {"account": account, "token": SecretValue(f"tok-{account}")}

            await app.preset_manager.register(
                "x_vault", "vault", {"account": "x"}, [], "Short vault", output_schema=schema
            )
            server_logger.addHandler(capture)
            try:
                async with Client(app._fast_mcp) as client:
                    with pytest.raises(ToolError) as caught:
                        await client.call_tool("x_vault", {})
            finally:
                server_logger.removeHandler(capture)
                await app.preset_manager.remove("x_vault")

            client_error = str(caught.value)
            # The client-visible error carries the redacted failure, never the real token.
            assert "tok-x" not in client_error
            assert "$.token" in client_error
            assert SECRET_PLACEHOLDER in client_error

            # The failure WAS logged (not swallowed), and the real token appears in NO
            # log record — neither its message nor the attached traceback.
            assert capture.texts, "expected fastmcp to log the failed tool call"
            assert all("tok-x" not in text for text in capture.texts)

    asyncio.run(run())


def test_mcp_edge_non_secret_preset_schema_violation_quotes_the_offending_instance():
    # A NON-secret preset over the same live MCP edge whose plain result violates its
    # output_schema: no secret was revealed on this door, so the secret redaction does
    # NOT widen here — the raised error still quotes the offending instance verbatim.
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "units": {"type": "string", "maxLength": 2}},
        "required": ["city", "units"],
    }

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def weather(city: str, units: str = "metric") -> dict:
                """Report the weather for a city."""
                return {"city": city, "units": units}

            await app.preset_manager.register(
                "p_bad", "weather", {"units": "imperial"}, [], "bad", output_schema=schema
            )
            try:
                async with Client(app._fast_mcp) as client:
                    with pytest.raises(ToolError) as caught:
                        await client.call_tool("p_bad", {"city": "x"})
            finally:
                await app.preset_manager.remove("p_bad")

            # The baked ``imperial`` (7 chars) is the offending value; the non-secret
            # door keeps instance-quoting, so it rides the error unchanged.
            assert "imperial" in str(caught.value)

    asyncio.run(run())


def test_offloaded_sync_tool_outruns_cancellation_and_completes_in_background(set_turn_timeout):
    # Python cannot cancel a running thread: when a plain (sync) tool is offloaded via
    # ``asyncio.to_thread`` and the budget expires, the awaiting caller is answered with
    # ``TurnTimeoutError``, but the thread runs to completion in the background — the real
    # boundary of the turn budget.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("blocking_sync_tool")):
            with pytest.raises(TurnTimeoutError, match=r"turn exceeded the 0\.05s turn timeout"):
                await app.tools.run_tool("blocking_sync_tool", {"seconds": 0.4}, offload_sync=True)

    asyncio.run(run())
    # The caller was answered on expiry, yet the offloaded thread could not be interrupted
    # and recorded its completion in the background.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _budget_flag("blocking_sync_completions"):
        time.sleep(0.02)
    assert list(_budget_flag("blocking_sync_completions")) == ["blocking_sync_tool"]
