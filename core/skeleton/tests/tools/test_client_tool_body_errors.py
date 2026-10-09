"""The client-tool handle's error semantics.

A tool body's OWN failure while doing its job becomes a typed, model-visible
``ClientToolError`` (the same message shape a FastMCP boundary uses), so an agent's
tool loop surfaces it instead of aborting. Platform-machinery failures — a vanished
registration re-resolved on the live-fire path, an identity-gate denial — stay raw
loud aborts, and cancellation passes through untouched.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic import BaseModel
from tai42_contract.tools import TOOL_META_PAUSES, ClientTool, ClientToolError

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding import UnknownToolError


class _PydanticOut(BaseModel):
    """A tool-return model used to prove a pydantic field's surrogate does not slip through."""

    name: str


class _SurrogateKeyModel(BaseModel):
    """A model whose ``data`` field is a dict keyed by a surrogate — un-encodable, not deep-walked."""

    data: dict


async def _handle(name: str) -> ClientTool:
    [handle] = await app.tools.get_client_tools([name])
    return handle


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def test_tool_body_exception_becomes_client_tool_error(caplog):
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def boom(q: str) -> str:
                """A tool whose body raises while doing its job."""
                raise ValueError("kaboom")

            handle = await _handle("boom")

            with (
                caplog.at_level(logging.WARNING, logger="tai42_skeleton.tools.binding.client_tools"),
                pytest.raises(ClientToolError, match=r"Error calling tool 'boom': kaboom"),
            ):
                await handle.invoke({"q": "x"})

            # The server-side trace is not silenced: the binding logs the failure
            # naming the tool and the error.
            assert any(
                r.name == "tai42_skeleton.tools.binding.client_tools"
                and "boom" in r.getMessage()
                and "kaboom" in r.getMessage()
                for r in caplog.records
            )

    asyncio.run(run())


def test_machinery_failure_on_live_fire_propagates_unchanged(monkeypatch):
    # The live-fire branch re-resolves the target; a vanished registration is
    # platform machinery, so its UnknownToolError propagates raw — never a tool error.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def gone(q: str) -> str:
                """A tool that would succeed if it still existed."""
                return q

            handle = await _handle("gone")

            # Enter the identity-bound branch, then vanish the registration so the
            # re-resolution fails as machinery before the body is ever reached.
            monkeypatch.setattr(app._tool_binding, "_bound_execution_identity", lambda: object())
            app._tool_binding.remove_tool("gone")

            with pytest.raises(UnknownToolError):
                await handle.invoke({"q": "x"})

    asyncio.run(run())


def test_identity_gate_denial_propagates_unchanged(monkeypatch):
    # An identity-gate denial is platform machinery: its own exception type propagates
    # raw, never wrapped as a tool error.
    class GateDeniedError(Exception):
        pass

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def guarded(q: str) -> str:
                """A tool the identity gate refuses to dispatch."""
                return q

            handle = await _handle("guarded")

            async def _deny(*args, **kwargs):
                raise GateDeniedError("not allowed")

            monkeypatch.setattr(app._tool_binding, "_bound_execution_identity", lambda: object())
            monkeypatch.setattr(app._tool_binding, "_authorize_execution_dispatch", _deny)

            with pytest.raises(GateDeniedError):
                await handle.invoke({"q": "x"})

    asyncio.run(run())


def test_cancelled_error_passes_through_untouched():
    # CancelledError is a BaseException, never converted: the ``except Exception`` wrap
    # leaves it untouched.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def cancels(q: str) -> str:
                """A tool whose body is cancelled mid-run."""
                raise asyncio.CancelledError

            handle = await _handle("cancels")

            with pytest.raises(asyncio.CancelledError):
                await handle.invoke({"q": "x"})

    asyncio.run(run())


def test_a_declared_park_returns_the_suspended_interaction_unadapted():
    # An async ask parks the caller and returns a SuspendedInteraction sentinel. The handle hands
    # it back BY TYPE with its owner and both id lists: adopting it is the engine adapter's job.
    from datetime import UTC, datetime

    from tai42_contract.interactions import (
        SuspendedInteraction,
        get_resume_continuation_tool,
        reset_resume_continuation_tool,
        set_resume_continuation_tool,
    )

    deadline = datetime(2030, 1, 1, tzinfo=UTC)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
            async def parks(q: str):
                """A tool that async-parks and returns the suspension sentinel."""
                return SuspendedInteraction(
                    interaction_id="i1",
                    expiry_at=deadline,
                    resume_owner=get_resume_continuation_tool(),
                    interaction_ids=["i1", "i2"],
                    caller_interaction_ids=["i2"],
                )

            handle = await _handle("parks")
            token = set_resume_continuation_tool("agent_resume")
            try:
                result = await handle.invoke({"q": "x"})
            finally:
                reset_resume_continuation_tool(token)
            assert result == SuspendedInteraction(
                interaction_id="i1",
                expiry_at=deadline,
                resume_owner="agent_resume",
                interaction_ids=["i1", "i2"],
                caller_interaction_ids=["i2"],
            )

    asyncio.run(run())


def test_unencodable_result_becomes_a_model_visible_client_tool_error():
    # A tool whose return holds a lone surrogate is refused as a model-visible ClientToolError
    # naming the tool and the JSON path — never a crash when an engine encodes it.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def emits() -> dict:
                """A tool whose dict value holds a lone surrogate."""
                return {"token": "\ud83d"}

            handle = await _handle("emits")

            with pytest.raises(ClientToolError, match=r"emits.*cannot be JSON-encoded at \$\.token"):
                await handle.invoke({})

    asyncio.run(run())


def test_a_pydantic_return_with_a_surrogate_is_refused():
    # The RAW return is reduced FIRST, so a pydantic model whose field holds a surrogate
    # cannot slip past the walk.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def emits() -> _PydanticOut:
                """A tool returning a pydantic model whose field holds a lone surrogate."""
                return _PydanticOut(name="\ud83d")

            handle = await _handle("emits")

            with pytest.raises(ClientToolError, match=r"emits.*cannot be JSON-encoded at \$\.name"):
                await handle.invoke({})

    asyncio.run(run())


def test_an_unencodable_model_leaf_becomes_a_model_visible_client_tool_error():
    # A leaf the encoder cannot render (a model holding a surrogate dict key) is refused as a
    # model-visible ClientToolError naming the tool and the FIELD path — the model is not
    # deep-reduced, and the reduction never crashes raw into the agent loop.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def emits() -> dict:
                """A tool whose payload field is a model the encoder cannot render."""
                return {"payload": _SurrogateKeyModel(data={"\ud83d": "v"})}

            handle = await _handle("emits")

            with pytest.raises(ClientToolError, match=r"emits.*cannot be JSON-encoded at \$\.payload"):
                await handle.invoke({})

    asyncio.run(run())


def test_an_encodable_emoji_return_passes_through():
    # A real, encodable emoji is not flagged: the tool returns its value to the model.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def emits() -> dict:
                """A tool returning a real, encodable emoji."""
                return {"emoji": "😀"}

            handle = await _handle("emits")

            assert await handle.invoke({}) == {"emoji": "😀"}

    asyncio.run(run())


def test_tool_body_failed_terminal_renders_the_whole_outcome_as_text():
    # A driver's FAILED terminal RAISES ``RunTerminalFailed`` carrying the outcome WHOLE. The
    # in-process agent tool door renders the whole opaque outcome as JSON TEXT on the
    # ``ClientToolError`` (reading no key inside it), so the model reads the driver's own graceful
    # surface — the deep nested marker, well past the 200-char record cap, present and untruncated —
    # not the bare contract message the generic arm would collapse it to.
    import json

    from tai42_contract.interactions import RunTerminalFailed
    from tai42_kit.interactions.outcome_text import failed_outcome_text

    from tai42_skeleton.interactions.terminal_failure import FAILED_OUTCOME_DETAIL_LIMIT, failed_outcome_detail

    outcome = {
        "status": "error",
        "result": {"on_error": [{"step": "retry", "note": "N" * 300}], "graceful": "DEEP_MARKER_PAST_200"},
    }
    # The marker genuinely sits past the record cap, so a capped repr could not carry it.
    capped = failed_outcome_detail(outcome)
    assert "DEEP_MARKER_PAST_200" not in capped
    assert len(capped) <= len("tool run failed: ") + FAILED_OUTCOME_DETAIL_LIMIT + len("…(truncated)")

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def flow() -> str:
                """A driver whose body raises a failed terminal carrying a whole outcome."""
                raise RunTerminalFailed(outcome)

            handle = await _handle("flow")

            with pytest.raises(ClientToolError) as caught:
                await handle.invoke({})
            # The ClientToolError carries the WHOLE outcome as JSON text (via the contract helper).
            assert str(caught.value) == failed_outcome_text(outcome)
            assert json.loads(str(caught.value)) == outcome
            assert "DEEP_MARKER_PAST_200" in str(caught.value)

    asyncio.run(run())
