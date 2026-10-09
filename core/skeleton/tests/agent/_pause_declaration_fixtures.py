"""Neutral synthetic agent kinds for the run tool's pause declaration, derived from ``Agent.parks``.

``parking_kind`` declares ``parks = True``; ``still_kind`` keeps the default ``False`` though its run
returns a park — so the platform refuses that park as undeclared. Both name no engine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from tai42_contract.app import tai42_app
from tai42_contract.interactions import SuspendedInteraction
from tests.agent._memoryless import MemorylessTestAgent


class _Input(BaseModel):
    text: str = ""
    # Advertised so a preset declaring an ``output_schema`` can bake the forced structured output.
    response_format: dict[str, Any] | None = None


def _park(interaction_id: str) -> SuspendedInteraction:
    return SuspendedInteraction(interaction_id=interaction_id, expiry_at=datetime.now(UTC) + timedelta(hours=1))


@tai42_app.agents.agent("parking_kind")
class ParkingKind(MemorylessTestAgent):
    tool_name = "parking_kind"
    tool_description = "A kind whose runs can park."
    ToolInput = _Input
    parks = True

    async def run(self, **_: Any) -> SuspendedInteraction:
        return _park("parked-run")


@tai42_app.agents.agent("still_kind")
class StillKind(MemorylessTestAgent):
    tool_name = "still_kind"
    tool_description = "A kind that declares it never parks."
    ToolInput = _Input

    async def run(self, **_: Any) -> SuspendedInteraction:
        return _park("undeclared-run")
