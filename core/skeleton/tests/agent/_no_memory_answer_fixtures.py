"""A fake agent class that omits ``Agent.addressed_memory`` — registration must refuse it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.app import tai42_app


class _Input(BaseModel):
    text: str = ""


@tai42_app.agents.agent("no_memory_answer")
class NoMemoryAnswerAgent(Agent):  # type: ignore[abstract]
    tool_name = "no_memory_answer"
    tool_description = "Answers nothing about the memory its run kwargs address."
    ToolInput = _Input

    async def run(self, **_: Any) -> str:
        return ""
