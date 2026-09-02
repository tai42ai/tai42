"""Probe for the delivery-scope rule, shared by every agent that assembles a tool list.

The rule (``tai42_agents._internal.nested_dispatch``): a tool an agent dispatches is a STEP of
that agent's turn, never a second answerer of it, so its body never runs under the door's
park-completion binding — a parking driver reached through the tool cannot read the address the
conversation door bound for the agent's own deferred answer.

The probe below runs the handed tool OUTSIDE a park-capable drive (no resume continuation
bound), which is the case where the dispatch binds nothing at all. A park-capable drive binds
the CHAINED completion instead, which is equally unreachable to the door's address (it embeds
it); that half is pinned over real graphs in ``tests/test_nested_dispatch.py``.

Each agent builds its tool list at its own seam, so the rule is asserted per seam: hand the
agent a :func:`probe_tool`, capture the tool the agent actually handed its graph, and run it
through :func:`assert_delivery_scoped`.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from tai42_contract.interactions import get_park_completion, reset_park_completion, set_park_completion

# A bound completion standing in for what the conversation door binds around an agent turn.
BOUND_COMPLETION: tuple[str, dict[str, str]] = ("conversation_deliver", {"thread_id": "bridge:acme:alice"})


def probe_tool(name: str = "probe") -> tuple[StructuredTool, list[tuple[str | None, Any]]]:
    """A tool whose body records the park completion it can see, plus the recording list."""
    seen: list[tuple[str | None, Any]] = []

    def _peek() -> str:
        seen.append(get_park_completion())
        return "peeked"

    return StructuredTool.from_function(func=_peek, name=name, description="a probe tool"), seen


def assert_delivery_scoped(handed: StructuredTool, seen: list[tuple[str | None, Any]]) -> None:
    """Run the tool the agent handed its graph under a BOUND completion and assert its body saw
    none of it — and that the caller's own binding survives the dispatch untouched."""
    token = set_park_completion(*BOUND_COMPLETION)
    try:
        assert handed.func is not None
        handed.func()
        assert get_park_completion() == BOUND_COMPLETION
    finally:
        reset_park_completion(token)
    assert seen == [(None, None)]
