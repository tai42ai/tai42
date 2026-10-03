"""The agent tool-face delivers a park as the typed ``SuspendedInteraction`` by TYPE.

A parked run leaves ``Agent.run`` as a ``SuspendedInteraction`` typed value; the synthesized run
tool passes it through unchanged. A run whose own ANSWER is a plain dict carrying a ``status`` key
is delivered as a SUCCESS — the face never inspects a returned value for a status key.
"""

from __future__ import annotations

import asyncio

from tai42_contract.interactions import SuspendedInteraction

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest


def _manifest(*agents: str) -> Manifest:
    return Manifest.model_validate(
        {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": list(agents)}]}
    )


def test_a_parked_run_crosses_the_tool_face_as_the_typed_sentinel() -> None:
    # The agent parked: its ``run`` returns a SuspendedInteraction, and the run tool passes the
    # typed value through unchanged so a caller recognizes the park by TYPE.
    async def go() -> None:
        async with app.app_context(_manifest("parking")):
            result = await app.tools.run_tool("parking", {"text": "go"})
            assert isinstance(result, SuspendedInteraction)
            assert result.interaction_id == "i-parked"
            assert result.interaction_ids == ["i-parked"]
            assert result.caller_interaction_ids == []

    asyncio.run(go())


def test_a_plain_dict_answer_with_a_status_key_is_delivered_as_a_success() -> None:
    # The run did NOT park — its answer is a plain dict that happens to carry ``status:"suspended"``.
    # The face must deliver it as a SUCCESS, never mistaking a status key in a returned value for a
    # park (which once KeyError'd on the absent ``interaction_ids`` / built a bogus sentinel).
    async def go() -> None:
        async with app.app_context(_manifest("status_answer")):
            result = await app.tools.run_tool("status_answer", {"text": "go"})
            assert result == {"status": "suspended", "data": "this is the real answer"}

    asyncio.run(go())
