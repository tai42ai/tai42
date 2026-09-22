"""A dummy :class:`Agent` declaring one door-``extras`` key.

Listed as an ``agents:`` manifest module so importing it fires ``@tai42_app.agents.agent`` and the
skeleton registers the agent. Its ``extras_keys`` names the single key a door start may carry, so
the shared visit's extras pre-check has an AGENT target to resolve declared keys from.
"""

from typing import Any, ClassVar

from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.app import tai42_app


class _ExtrasInput(BaseModel):
    text: str = ""


@tai42_app.agents.agent("extras_agent")
class ExtrasAgent(Agent):
    tool_name = "extras_agent"
    tool_description = "Declares one door-extras key."
    ToolInput = _ExtrasInput
    extras_keys: ClassVar[frozenset[str]] = frozenset({"declared"})

    async def run(self, *, text: str = "", **_: Any) -> Any:
        return text
