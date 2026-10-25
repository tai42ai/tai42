"""A registered AGENT with an optional field, run on a backend worker through its ``sync_task`` branch.

Its run tool presents one parameter per ``ToolInput`` field, so a caller that leaves ``count`` out
reaches the branch with the omitted-value marker for it. The run answers ``"<text>:<count>"``, so
a caller reads which value the worker ran with: the agent's own default when nothing shipped for
``count``. No model and no memory.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel
from tai42_contract.agent import AddressedMemory, Agent
from tai42_contract.app import tai42_app

AGENT_NAME = "e2e_fields_agent"


class _FieldsInput(BaseModel):
    """A required field and an optional one with its own default."""

    text: str
    count: int = 3


@tai42_app.agents.agent(AGENT_NAME, tags={"e2e"})
class E2eFieldsAgent(Agent):
    """Answer ``"<text>:<count>"`` for the fields the run received."""

    tool_name: ClassVar[str] = AGENT_NAME
    tool_description: ClassVar[str] = "E2E probe agent: answers its text and count fields as 'text:count'."
    ToolInput: ClassVar[type[BaseModel]] = _FieldsInput

    @classmethod
    def addressed_memory(cls, run_kwargs: Mapping[str, Any]) -> AddressedMemory:
        """No memory: every run is addressed to none."""
        return AddressedMemory()

    async def run(self, *, text: str, count: int = 3, **kwargs: Any) -> str:
        return f"{text}:{count}"
