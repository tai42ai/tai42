"""The structured-output re-prompt counter belongs to the DRIVE, not to the compiled graph.

One compiled graph may serve many runs, so each drive binds its own counters
(:func:`drive_reprompt_scope`) and every rail counts on the entry it owns in them. Driving a
rail-holding graph with no scope bound is a loud ``RuntimeError``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from tai42_contract.agent.events import StructuredOutputUnresolvedFinal

from tai42_agents._internal.outcomes import RepromptCapError, drive_reprompt_scope
from tai42_agents._internal.structured import ainvoke_structured
from tai42_agents._internal.structured_rail import StructuredOutputRailMiddleware

from .test_structured_completion import _NativeFake
from .test_structured_reprompt_cap import _SCHEMA, _answer_call, _compile, _project, _ScriptedModel

_CAP = 3


@pytest.fixture(autouse=True)
def _cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=_CAP),
    )


def _config(thread: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}, "recursion_limit": 50}


def test_one_compiled_graph_driven_twice_gives_each_drive_the_full_cap() -> None:
    model = _ScriptedModel([_answer_call("not-an-int")])
    graph, strategy = _compile(model, [], _SCHEMA)

    first = asyncio.run(_project(graph, strategy, _SCHEMA, _config("t1")))
    second = asyncio.run(_project(graph, strategy, _SCHEMA, _config("t2")))

    for events in (first, second):
        terminal = events[-1]
        assert isinstance(terminal, StructuredOutputUnresolvedFinal)
        assert terminal.attempts == _CAP + 1
    assert model.calls == 2 * (_CAP + 1)


def test_two_concurrent_drives_of_one_graph_count_separately() -> None:
    model = _ScriptedModel([_answer_call("not-an-int")])
    graph, strategy = _compile(model, [], _SCHEMA)

    async def both() -> list[list[Any]]:
        return list(
            await asyncio.gather(
                _project(graph, strategy, _SCHEMA, _config("a")),
                _project(graph, strategy, _SCHEMA, _config("b")),
            )
        )

    for events in asyncio.run(both()):
        terminal = events[-1]
        assert isinstance(terminal, StructuredOutputUnresolvedFinal)
        assert terminal.attempts == _CAP + 1


def test_a_rail_driven_with_no_scope_raises() -> None:
    model = _ScriptedModel([_answer_call(7)])
    graph, _strategy = _compile(model, [], _SCHEMA)

    with pytest.raises(RuntimeError, match="structured-output rail ran outside a drive scope"):
        asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}, _config("t")))


def test_two_rails_in_one_drive_count_separately() -> None:
    first = StructuredOutputRailMiddleware(_SCHEMA, cap=_CAP)
    second = StructuredOutputRailMiddleware(_SCHEMA, cap=_CAP)
    error = ValueError("off schema")

    with drive_reprompt_scope():
        for _ in range(_CAP):
            first._reprompt(error)
        with pytest.raises(RepromptCapError) as tripped:
            first._reprompt(error)
        assert tripped.value.attempts == _CAP + 1
        # The second rail's counter is its own: it still re-prompts.
        assert second._reprompt(error)


def test_a_drive_scope_starts_every_rail_at_zero() -> None:
    rail = StructuredOutputRailMiddleware(_SCHEMA, cap=_CAP)
    error = ValueError("off schema")

    with drive_reprompt_scope():
        for _ in range(_CAP):
            rail._reprompt(error)
    with drive_reprompt_scope():
        for _ in range(_CAP):
            assert rail._reprompt(error)


def test_the_single_shot_structured_call_keeps_its_own_cap_with_no_scope() -> None:
    fake = _NativeFake(['{"value": "nope"}'])

    async def run() -> Any:
        from tai42_kit.llm.structured import plan_structured_output

        llm: Any = fake
        plan = plan_structured_output(llm, "openai", _SCHEMA)
        return await ainvoke_structured(llm, plan, [HumanMessage(content="go")])

    with pytest.raises(RepromptCapError) as tripped:
        asyncio.run(run())
    assert tripped.value.attempts == _CAP + 1
    assert fake.calls == _CAP + 1
