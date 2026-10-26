"""Cross-driver chain routing across a NESTED tool dispatch.

An agent dispatches a tool as a STEP of its own turn — whatever the tool resolves to belongs to
the agent, which folds it into the answer it alone delivers. The door's out-of-band delivery
address rides ``_park_completion`` and flows DOWN unchanged through every dispatch: the platform
delivers each run's outcome to that run's OWN stored address, so no nested driver can hijack the
agent's answer by capturing the address (no driver fires the door completion).

What a nested dispatch DOES bind is the cross-driver CHAIN ROUTING, on ``chained_resume``, and
there are two answers.

CHAINED — the dispatch of a run that can park. ``chained_resume`` is set to a fresh key naming
this call, the chain-delivery tool that re-enters this loop, and the ancestor's own call chain. A
nested driver that parks under it captures that routing, so when it reaches its terminal it fires
the chain's delivery tool, which re-enters this loop with that terminal as the tool's result. The
agent parks on the CALL — a park of its own, owned by its own resume continuation — while the
nested run keeps its park and its resume. This is what lets an agent wait on a composed tool (or another
agent) that has to ask a human.

CLEARED — the dispatch of a run that cannot park. Nothing here could wait on a nested terminal,
so ``chained_resume`` is set to ``None``: a nested run's park is refused to the model by the
ownership guard, loudly, rather than suspending a run with no way back.

The two are the same rule under different capabilities, read off the ONE fact that decides it:
whether a resume continuation is bound for this run. Nothing else here knows about parking.

One chained key addresses one CALL, so a single dispatch that reaches two parking runs can only
be waited on for the first: the second claims the same key and would be delivered to the same
park. The seams this plugin owns cannot produce that shape — a tool body that parks returns its
park as its result, ending the dispatch — and a composite assembled elsewhere is the boundary
below.

The reach is this plugin's own dispatch seams, and there is a boundary beyond them. A composite
toolbox tool an agent calls is covered, because the agent dispatches the composite and the whole
chain runs inside this scope. A chain or batch step that is itself a foreign parking driver,
assembled OUTSIDE an agent (a toolbox chain wired straight onto a conversation route), is the
same hazard class under a different owner and out of this plugin's reach — the scope has to be
applied by whoever dispatches the step.

The routing is bound for the DISPATCH ONLY, so the agent's own park — raised by the graph outside
any tool body — captures no chain and is delivered by the platform to the run's own address.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from tai42_contract.interactions import (
    ChainedResume,
    get_resume_continuation_tool,
    reset_chained_resume,
    set_chained_resume,
)
from tai42_contract.tools import current_call_chain
from tai42_kit.interactions.park_adoption import new_chained_park_key

from tai42_agents._internal.park import AGENT_RESUME_TOOL_NAME
from tai42_agents._internal.park.chain import CHAINED_PARK_DELIVERY_TOOL_NAME


@contextlib.contextmanager
def nested_tool_dispatch(*, chain: bool = False) -> Iterator[None]:
    """Run a nested tool dispatch under its own chain routing.

    Wraps the exact call that hands control to a foreign tool body, and restores the caller's own
    routing in a ``finally``. The door's out-of-band delivery address (``_park_completion``) is NOT
    touched — it flows DOWN unchanged, because no driver fires the door completion (the
    platform delivers to the run's own address), so no driver can hijack another run's delivery by
    overwriting that address.

    ``chain`` asks for the CHAINED resume routing: a fresh chained key addressing this one call,
    carried on ``chained_resume`` with the ancestor's own call chain, so a nested run that parks
    captures it and re-enters THIS loop with its terminal (through ``deliver_chained_park``) instead
    of stranding it. The key joins a park's lineage only when a nested driver binds its resume
    continuation under this routing (the nested run's face); an ``ask`` this tool body raises
    directly is the run's own park and records nothing of the key, so a kill of such a park never
    looks for an owner of it. It is honored only when a resume continuation is bound for this run
    (``agent_resume``) — exactly the condition under which this run can park at all, and a chain
    nothing can park on would leave the nested run firing at a key that never existed. Otherwise,
    and by default, ``chained_resume`` is CLEARED to ``None`` so a nested run that cannot be waited
    on captures no stale chain of an outer dispatch.
    """
    if chain and get_resume_continuation_tool() == AGENT_RESUME_TOOL_NAME:
        # ``asked_by`` is the ancestor's OWN call chain at this dispatch, passed as
        # ``continues_chain`` on the chain re-entry so the ancestor's re-park records its own chain
        # rather than the descendant's plus the chain tool's name.
        routing: ChainedResume | None = ChainedResume(
            delivery_tool=CHAINED_PARK_DELIVERY_TOOL_NAME,
            chain_key=new_chained_park_key(),
            asked_by=current_call_chain(),
        )
    else:
        routing = None
    token = set_chained_resume(routing)
    try:
        yield
    finally:
        reset_chained_resume(token)
