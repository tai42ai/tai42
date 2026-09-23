"""Fixture continuation + target tools for the shared visit's inline-resume drive tests.

``resume_tool`` is a REAL registered continuation face the visit drives through ``run_tool``: it
branches on the resume ``answer`` to return each outcome shape a driver can produce (a terminal
result, a re-park sentinel, a ``ResumeBuffered`` partial, a ``ParkResumeFailed`` terminal, or a
plain raise), and records whether an ambient door ``ToolInvocation`` binding was present when it
ran (it must NOT be — the visit binds a door only around a START). ``extras_target`` declares one
extras key so the undeclared-extras pre-check has a target to check against.
"""

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    ParkResumeFailed,
    ResumeBuffered,
    SuspendedInteraction,
    get_resume_continuation_tool,
)
from tai42_contract.tools import current_tool_invocation


@tai42_app.tools.tool
async def resume_tool(interaction_id: str, answer: Any) -> Any:
    """A continuation face: branch on ``answer`` to return each outcome shape a driver produces.

    A terminal result records whether an ambient door ``ToolInvocation`` binding was present when it
    ran (``door_binding_present``), so a test asserts a resume continuation ran WITHOUT the door's
    state binding (the visit binds a door only around a START).
    """
    invocation = current_tool_invocation()
    door_binding_present = invocation is not None and invocation.state_binding is not None
    kind = answer.get("kind") if isinstance(answer, dict) else None
    if kind == "repark_user":
        return SuspendedInteraction(
            interaction_id=answer["new_id"], resume_owner=get_resume_continuation_tool(), caller_interaction_ids=[]
        )
    if kind == "repark_caller":
        return SuspendedInteraction(
            interaction_id=answer["new_id"],
            interaction_ids=[answer["new_id"]],
            caller_interaction_ids=[answer["new_id"]],
            resume_owner=get_resume_continuation_tool(),
        )
    if kind == "buffered":
        return ResumeBuffered(remaining_ids=answer["remaining"])
    if kind == "park_failed":
        raise ParkResumeFailed({"status": "failed", "reason": answer.get("reason", "aborted")})
    if kind == "boom":
        raise RuntimeError("resume drive blew up")
    return {
        "status": "success",
        "value": answer.get("value") if isinstance(answer, dict) else answer,
        "door_binding_present": door_binding_present,
    }


@tai42_app.tools.tool(extras_keys=frozenset({"declared"}))
async def extras_target(x: int = 0) -> dict[str, Any]:
    """A target that declares the extras key ``declared`` (for the undeclared-extras pre-check)."""
    return {"x": x, "extras": dict(tai42_app.tools.extras())}
