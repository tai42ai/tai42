"""The agent base's declared door-``extras`` keys."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from tai42_contract.agent.base import Agent


def test_agent_extras_keys_defaults_to_empty():
    assert Agent.extras_keys == frozenset()


def test_agent_subclass_declares_extras_keys():
    class _Reader(Agent):
        tool_name = "reader"
        ToolInput = BaseModel
        extras_keys: ClassVar[frozenset[str]] = frozenset({"warm_start"})

        async def run(self, **kwargs: Any) -> Any:
            return None

    assert _Reader.extras_keys == frozenset({"warm_start"})
    # A subclass that declares none keeps the empty default.

    class _Plain(Agent):
        tool_name = "plain"
        ToolInput = BaseModel

        async def run(self, **kwargs: Any) -> Any:
            return None

    assert _Plain.extras_keys == frozenset()
