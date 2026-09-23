"""Probe for the delivery-scope rule, shared by every agent that assembles a tool list.

The rule (``tai42_agents._internal.nested_dispatch``): a tool an agent dispatches is a STEP of
that agent's turn. The door's out-of-band delivery address flows DOWN unchanged through the
dispatch (the platform delivers each run's outcome to its own stored address, so no nested driver
can hijack the agent's answer). What the dispatch binds instead is the cross-driver CHAIN routing
on ``chained_resume``: a park-capable drive binds a chain the nested run can re-enter through, and
every OTHER dispatch clears ``chained_resume`` to ``None`` so a nested run that cannot be waited on
captures no stale chain of an outer dispatch.

The probe below runs the handed tool OUTSIDE a park-capable drive (no resume continuation bound),
which is the case where the dispatch binds NO chain: its body sees ``chained_resume`` cleared to
``None``. The park-capable half — a real chain bound — is pinned over real graphs in
``tests/test_nested_dispatch.py``.

Each agent builds its tool list at its own seam, so the rule is asserted per seam: hand the
agent a :func:`probe_tool`, capture the tool the agent actually handed its graph, and run it
through :func:`assert_delivery_scoped`.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from tai42_contract.interactions import (
    ChainedResume,
    get_chained_resume,
    get_park_completion,
    reset_park_completion,
    set_park_completion,
)

# A bound completion standing in for what the conversation door binds around an agent turn.
BOUND_COMPLETION: tuple[str, dict[str, str]] = ("conversation_deliver", {"thread_id": "bridge:acme:alice"})


def probe_tool(name: str = "probe") -> tuple[StructuredTool, list[ChainedResume | None]]:
    """A tool whose body records the chain routing it can see, plus the recording list."""
    seen: list[ChainedResume | None] = []

    def _peek() -> str:
        seen.append(get_chained_resume())
        return "peeked"

    return StructuredTool.from_function(func=_peek, name=name, description="a probe tool"), seen


def assert_delivery_scoped(handed: StructuredTool, seen: list[ChainedResume | None]) -> None:
    """Run the handed tool under a bound door completion and assert its body captured NO chain.

    Outside a park-capable drive the dispatch binds no chain, so the body sees ``chained_resume``
    cleared to ``None``; the door's out-of-band address (``_park_completion``) flows down unchanged,
    surviving the dispatch untouched.
    """
    token = set_park_completion(*BOUND_COMPLETION)
    try:
        assert handed.func is not None
        handed.func()
        assert get_park_completion() == BOUND_COMPLETION
    finally:
        reset_park_completion(token)
    assert seen == [None]
