"""The ``set_conversation_mode`` builtin: flip the CURRENT conversation's control mode
between ``agent`` (the target turn runs) and ``manual`` (an operator answers by hand), so an
agent can hand its own conversation off to a human.

A thin, LLM-facing shim over
:func:`tai42_skeleton.conversations.mode.set_current_thread_mode`. It learns which thread to
flip from the turn-scoped bridge context the turn engine establishes around the agent
invocation — an in-process call within the agent turn, so the context propagates to it (the
same contextvar mechanism the bound execution identity uses). Called outside a bridge turn it
raises loudly; an external/programmatic caller sets a thread's mode through the
``PUT /thread/mode`` door instead, which names its thread explicitly.

Authorization fence: a plain builtin takes the capability path — no per-call scope check — so
its reach is bounded by manifest exposure (a deployment opts the module in through a
``tools[].module`` row) plus the tool's own refusals: a bad mode or a call outside a bridge
turn.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.conversations.mode import set_current_thread_mode


@tai42_app.tools.tool(tags={"conversations"})
async def set_conversation_mode(mode: str) -> dict[str, str]:
    """Set the current conversation's control mode.

    ``agent`` runs the target turn on the next inbound message (the default); ``manual``
    suppresses it so an operator answers by hand, while platform control turns (pairing, the
    first-contact greeting) still run. The override stands until it is set again or the thread
    is forgotten.

    Args:
        mode: The mode to set — ``agent`` or ``manual``.

    Returns:
        ``{"mode": <the mode now in force>}`` and nothing else.

    Raises:
        ValueError: A ``mode`` outside ``agent``/``manual``.
        NoBridgeTurnError: Called with no bridge turn in flight — there is no current
            conversation whose mode to set.
    """
    stored = await set_current_thread_mode(mode)
    return {"mode": stored}
