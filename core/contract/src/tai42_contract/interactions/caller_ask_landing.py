"""The caller-ask landing fact — whether a caller ask may land on the current door.

A door that starts a run which can host a caller ask declares, around the run, whether such an ask
has a path back: an answer mapped to the caller AND a resume of the parked run. The ask park seam
reads this ambient fact — a caller ask about to park where landing is declared ABSENT would park
nowhere, so the seam fails the run loudly instead of persisting a question nothing can resolve. A
door that declares nothing (the default, ``None``) makes no claim and nothing is refused on its
account.

Door-agnostic by construction: the value carries only whether a caller ask can land and a label for
the refusal message; it names no route field, no tool, and no engine. Each door computes the fact
from its own contract and sets it around the run; the out-of-band resume of a park re-establishes the
fact the park captured, so a re-ask during a resume is judged by the same declaration.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel

#: The builtin tool an agent/flow lists to ask its CALLER mid-run — a caller ask IS the ``ask`` tool
#: with ``to="caller"``. Named here so the landing rule, the seam's refusal and each door's fail-fast
#: all agree on the one tool the landing governs.
CALLER_ASK_TOOL = "ask"


def binds_caller_ask(tool_names: object) -> bool:
    """Whether a statically-known ``tool_names`` value names the caller-ask tool.

    A non-iterable (``None`` — an unresolved run-time tool set) or a string/bytes is treated as not
    binding it — the truthful answer, never a guess.
    """
    if not isinstance(tool_names, Iterable) or isinstance(tool_names, str | bytes):
        return False
    return CALLER_ASK_TOOL in tool_names


def caller_ask_no_landing_outcome(label: str) -> dict[str, Any]:
    """The opaque FAILED outcome for a caller ask with no landing on the door ``label``.

    Carried on :class:`~tai42_contract.interactions.RunTerminalFailed` and recorded WHOLE by whichever
    door surfaces the failure; no key is read out of it. It names the door and the caller-ask tool so
    the recorded detail says where the ask had nowhere to land.
    """
    return {
        "tai42:caller_ask_no_landing": True,
        "route": label,
        "tool": CALLER_ASK_TOOL,
        "message": (
            f"a caller ask (the {CALLER_ASK_TOOL!r} tool) cannot land on {label!r}: "
            "it declares no reply/resume path for the caller's answer"
        ),
    }


class CallerAskLanding(BaseModel):
    """Whether a caller ask may land on the door that declared it.

    ``can_land`` is the declared fact (both a reply path and a resume path exist); ``label`` is the
    door's own name for itself (a route name, say), carried only so the seam's refusal names where
    the ask had nowhere to land. JSON-serializable, so a park carries it across to its resume.
    """

    can_land: bool
    label: str


_caller_ask_landing: ContextVar[CallerAskLanding | None] = ContextVar("tai42_caller_ask_landing", default=None)


@contextmanager
def declare_caller_ask_landing(landing: CallerAskLanding | None) -> Generator[None]:
    """Declare the caller-ask landing for the duration of a run a door starts.

    ``None`` declares nothing — the door makes no claim. The previous value is restored on exit.
    """
    token = _caller_ask_landing.set(landing)
    try:
        yield
    finally:
        _caller_ask_landing.reset(token)


def current_caller_ask_landing() -> CallerAskLanding | None:
    """The caller-ask landing declared around the current run, or ``None`` when no door declared one."""
    return _caller_ask_landing.get()
