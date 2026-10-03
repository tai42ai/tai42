"""The agent base's declared fixed tool set."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from pydantic import BaseModel

from tai42_contract.agent.base import Agent


def test_agent_tool_names_defaults_to_none():
    # ``None`` declares an agent that resolves its tool set per call — nothing static is known.
    assert Agent.tool_names is None


def test_agent_subclass_declares_a_fixed_tool_set():
    class _Fixed(Agent):
        tool_name = "fixed"
        ToolInput = BaseModel
        tool_names: ClassVar[Sequence[str] | None] = ["ask"]

        async def run(self, **kwargs: Any) -> Any:
            return None

    assert _Fixed.tool_names == ["ask"]
    assert _Fixed().tool_names == ["ask"]

    # A subclass that declares none keeps the per-call default.
    class _PerCall(Agent):
        tool_name = "per-call"
        ToolInput = BaseModel

        async def run(self, **kwargs: Any) -> Any:
            return None

    assert _PerCall.tool_names is None
