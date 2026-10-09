"""A fake agent whose registration ``meta`` names the pause key itself — registration must refuse it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from tai42_contract.app import tai42_app
from tai42_contract.tools import TOOL_META_PAUSES
from tests.agent._memoryless import MemorylessTestAgent


class _Input(BaseModel):
    text: str = ""


@tai42_app.agents.agent("meta_pause_kind", meta={TOOL_META_PAUSES: True})
class MetaPauseKind(MemorylessTestAgent):
    tool_name = "meta_pause_kind"
    tool_description = "Declares its pause through meta instead of parks."
    ToolInput = _Input

    async def run(self, **_: Any) -> str:
        return ""
