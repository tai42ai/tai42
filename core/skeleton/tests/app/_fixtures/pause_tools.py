"""Test-only tools for the pause declaration: parking tools with and without ``TOOL_META_PAUSES``.

``BODY_RUNS`` (:mod:`tests.app._fixtures.pause_counter`) counts each body's runs, so a test proves an undeclared
park's body ran once.
"""

from datetime import UTC, datetime, timedelta

from tai42_contract.app import tai42_app
from tai42_contract.interactions import ResumeBuffered, SuspendedInteraction, get_resume_continuation_tool
from tai42_contract.tools import TOOL_META_PAUSES
from tests.app._fixtures.pause_counter import BODY_RUNS


def _park(interaction_id: str) -> SuspendedInteraction:
    return SuspendedInteraction(
        interaction_id=interaction_id,
        expiry_at=datetime.now(UTC) + timedelta(hours=1),
        resume_owner=get_resume_continuation_tool(),
    )


@tai42_app.tools.tool
async def undeclared_park(q: str) -> SuspendedInteraction:
    """Parks without declaring that it can pause."""
    BODY_RUNS["undeclared_park"] += 1
    return _park("undeclared")


@tai42_app.tools.tool
async def undeclared_buffered(q: str) -> ResumeBuffered:
    """Returns a buffered resume without declaring that it can pause."""
    BODY_RUNS["undeclared_buffered"] += 1
    return ResumeBuffered(remaining_ids=["other"])


@tai42_app.tools.tool(meta={TOOL_META_PAUSES: True})
async def declared_park(q: str) -> SuspendedInteraction:
    """Parks and declares that it can pause."""
    BODY_RUNS["declared_park"] += 1
    return _park("declared")


@tai42_app.tools.tool(meta={TOOL_META_PAUSES: True})
async def declared_buffered(q: str) -> ResumeBuffered:
    """Returns a buffered resume and declares that it can pause."""
    BODY_RUNS["declared_buffered"] += 1
    return ResumeBuffered(remaining_ids=["other"])


@tai42_app.tools.tool(meta={TOOL_META_PAUSES: True})
async def redispatcher(target: str) -> object:
    """Declared pausing: re-dispatches ``target`` through the shared seam and passes its result through."""
    BODY_RUNS["redispatcher"] += 1
    return await tai42_app.tools.run_tool(target, {"q": "nested"})


@tai42_app.tools.tool
def echo(text: str) -> str:
    """Returns its input."""
    return text
