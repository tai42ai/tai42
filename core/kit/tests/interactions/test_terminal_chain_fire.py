"""A chained run's terminal fire settles the result through the app's preset stages, then fires the settled result."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    CHAINED_PARK_TOKEN_KEY,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    ChainedResume,
    RunFailed,
)

from tai42_kit.interactions.park_adoption import fire_terminal_chain

_ROUTING = ChainedResume(delivery_tool="deliver_chained_park", chain_key="tai42:chained-park:k1", asked_by=("main",))


class _FakeApp:
    def __init__(self, settle: Any) -> None:
        self.settled: list[tuple[str, Any]] = []
        self.fired: list[tuple[str, dict[str, Any], tuple[str, ...] | None]] = []
        self._settle = settle

        async def settle_chained_terminal(chain_key: str, result: Any) -> Any:
            self.settled.append((chain_key, result))
            return self._settle(result)

        async def run_tool(
            tool: str, payload: dict[str, Any], *, continues_chain: tuple[str, ...] | None = None
        ) -> Any:
            self.fired.append((tool, payload, continues_chain))
            return {"outermost": True}

        self.interactions = SimpleNamespace(settle_chained_terminal=settle_chained_terminal)
        self.tools = SimpleNamespace(run_tool=run_tool)


async def test_the_settled_result_fires_along_the_routing() -> None:
    app = _FakeApp(lambda result: {"mapped": result})
    outcome = await fire_terminal_chain(_ROUTING, 7, app=app)  # pyright: ignore[reportArgumentType]
    assert outcome == {"outermost": True}
    assert app.settled == [("tai42:chained-park:k1", 7)]
    assert app.fired == [
        (
            "deliver_chained_park",
            {
                CHAINED_PARK_TOKEN_KEY: "tai42:chained-park:k1",
                "result": {"mapped": 7},
                "status": PARK_COMPLETION_SUCCEEDED,
            },
            ("main",),
        )
    ]


async def test_a_failed_settle_fires_the_failed_status() -> None:
    failure = RunFailed(outcome={"tai42:preset_output_failed": True, "preset": "p", "message": "m"})
    app = _FakeApp(lambda _result: failure)
    await fire_terminal_chain(_ROUTING, {"raw": 1}, app=app)  # pyright: ignore[reportArgumentType]
    assert app.fired[0][1] == {
        CHAINED_PARK_TOKEN_KEY: "tai42:chained-park:k1",
        "result": failure.outcome,
        "status": PARK_COMPLETION_FAILED,
    }


async def test_the_bound_app_is_the_default() -> None:
    app = _FakeApp(lambda result: result)
    with tai42_app.bound(app):
        assert await fire_terminal_chain(_ROUTING, "x") == {"outermost": True}
    assert app.settled == [("tai42:chained-park:k1", "x")]


@pytest.mark.parametrize("result", [None, {"a": 1}])
async def test_every_result_is_settled_before_the_fire(result: Any) -> None:
    app = _FakeApp(lambda value: value)
    await fire_terminal_chain(_ROUTING, result, app=app)  # pyright: ignore[reportArgumentType]
    assert app.settled == [("tai42:chained-park:k1", result)]
