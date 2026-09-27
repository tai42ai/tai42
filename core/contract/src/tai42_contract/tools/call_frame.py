"""The ambient tool call-chain, extras, and run-delivery context.

A context manager the platform opens at every place a tool or agent starts
running. It owns three ambient facts of the run in flight, on the ContextVar
copy-on-fork discipline so a nested dispatch and a spawned branch inherit them:

* ``call_chain`` — the tuple of tool/agent names from the outermost run down to
  the current dispatch, outermost first. A resume matches on the name each level
  knows, to any depth.
* ``extras`` — an opaque mapping a door carries alongside a dispatch.
* the RUN-DELIVERY context ``{run_delivery_id, delivery}`` — the run's single
  delivery identity (a ``uuid4`` minted once at the outermost run start) and the
  out-of-band delivery address of the door that started the run (or ``None`` for
  a receiver-less door). Every nested dispatch, parallel branch, and re-park of
  the run inherits the one identity and the one address.

The frame has THREE forms:

* PUSH (``continues_chain`` is ``None`` and ``name`` is not ``None``) — push
  ``name`` onto ``call_chain``: an ordinary tool or agent start.
* SET (``continues_chain`` is not ``None``) — set ``call_chain`` to
  ``tuple(continues_chain)`` and push nothing: a continuation dispatch restores
  the parked run's chain, so the continuation tool's own name is absent for that
  one dispatch.
* DOOR (``name`` is ``None`` and ``continues_chain`` is ``None``) — push nothing:
  a door that opens the frame around a turn whose inner tool dispatch pushes the
  chain entry itself. It opens ONLY on a fresh run context (an empty prior
  ``call_chain`` and no ambient run-delivery context); a door opened while a run is
  already in flight raises :class:`NestedDoorFrameError`.

A run START — ``continues_chain`` is ``None`` onto an EMPTY prior ``call_chain``,
whether it PUSHES a name or is the mint-only DOOR form — binds the run-delivery
context: it mints a fresh ``run_delivery_id`` and reads the door's address off
:func:`get_park_completion` (bound by the door just before the run started,
``None`` for a receiver-less door). It binds ONLY when none is already ambient,
so a nested dispatch and a resume (which re-establishes the stored context
first) never re-mint.

The channel lives in the CONTRACT so a reader and the door that arms it can sit
in different packages that share only :mod:`tai42_contract`.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from tai42_contract.interactions.continuation import get_park_completion


@dataclass(frozen=True, eq=False)
class RunDelivery:
    """The run's single delivery identity and the starting door's out-of-band address.

    ``run_delivery_id`` is the ``uuid4`` minted once at the outermost run start,
    shared by every nested dispatch, sibling branch, and re-park of the run so
    its single terminal delivers once. ``delivery`` is the ``(tool, context)``
    the starting door bound as the run's completion address, or ``None`` when the
    door has no receiver.
    """

    run_delivery_id: str
    delivery: tuple[str | None, Mapping[str, Any] | None] | None


class NestedDoorFrameError(RuntimeError):
    """A DOOR-form :func:`tool_call_frame` was opened on a non-fresh run context.

    A door frame is the outermost minting frame of a run: it opens only when no run is already in
    flight in the current context. This is raised when the door's task inherited a running run's
    ambient context — a non-empty call chain, or an already-bound run-delivery context — which
    means the task was spawned as a new root of execution WITHOUT a fresh context. Minting the
    run's delivery identity under an inherited one would key the new run's terminal to the caller's
    run instead of to the door's own completion, so the frame refuses to open.
    """


_call_chain: ContextVar[tuple[str, ...]] = ContextVar("tai42_call_chain", default=())
_extras: ContextVar[Mapping[str, Any] | None] = ContextVar("tai42_call_extras", default=None)
_run_delivery: ContextVar[RunDelivery | None] = ContextVar("tai42_run_delivery", default=None)


def current_call_chain() -> tuple[str, ...]:
    """The ambient tool/agent call chain, outermost first; empty outside any run."""
    return _call_chain.get()


def current_extras() -> Mapping[str, Any]:
    """The ambient dispatch extras, or an empty mapping when none is bound."""
    extras = _extras.get()
    return extras if extras is not None else {}


def get_run_delivery_id() -> str | None:
    """The current run's delivery identity, or ``None`` outside a bound run."""
    delivery = _run_delivery.get()
    return delivery.run_delivery_id if delivery is not None else None


def get_run_delivery() -> tuple[str | None, Mapping[str, Any] | None] | None:
    """The current run's out-of-band delivery address, or ``None`` when receiver-less or unbound."""
    delivery = _run_delivery.get()
    return delivery.delivery if delivery is not None else None


@contextmanager
def run_delivery(delivery: RunDelivery | None) -> Generator[None]:
    """Re-establish a stored run-delivery context as the ambient one for the wrapped drive.

    A continuation drive of a receiver-less resume (the detached answer/expiry/reaper drive, a
    hook/schedule inline resume) is a fresh outermost run start with NO ambient run-delivery
    context, yet it must keep the RESUMED run's own delivery identity and address so its terminal
    delivers under the same ``completion_id`` and a re-park inside stores the same pair. The
    chokepoint binds the interaction's stored :class:`RunDelivery` here BEFORE the drive, so the
    resume's outermost :func:`tool_call_frame` finds one already ambient and does NOT re-mint.
    ``None`` binds nothing (a run that stored no delivery context). ContextVar token discipline:
    reset in the ``finally``.
    """
    if delivery is None:
        yield
        return
    token = _run_delivery.set(delivery)
    try:
        yield
    finally:
        _run_delivery.reset(token)


@contextmanager
def tool_call_frame(
    name: str | None = None,
    extras: Mapping[str, Any] | None = None,
    *,
    continues_chain: Sequence[str] | None = None,
) -> Generator[None]:
    """Open a call frame for a tool/agent dispatch, restoring the prior context on exit.

    See the module docstring for the three chain forms and the run-delivery bind.
    ContextVar token discipline: every value set here is reset in the ``finally``.
    """
    prior_chain = _call_chain.get()
    if continues_chain is not None:
        new_chain = tuple(continues_chain)
    elif name is not None:
        new_chain = (*prior_chain, name)
    else:
        # DOOR form: the outermost minting frame a door opens around a turn/run whose inner
        # dispatch pushes the chain entry itself. It opens ONLY on a fresh run context; a
        # non-empty prior chain or an ambient run-delivery context means the door's task
        # inherited a running run's context (a root spawned without a fresh context), which
        # would mis-key the new run's terminal to the caller's run. Refuse loudly.
        ambient_delivery = _run_delivery.get()
        if prior_chain or ambient_delivery is not None:
            raise NestedDoorFrameError(
                "a door frame opens only on a fresh run context; the door's task inherited a "
                f"running run's context (call chain {prior_chain!r}, run delivery id "
                f"{ambient_delivery.run_delivery_id if ambient_delivery is not None else None!r})"
            )
        new_chain = prior_chain

    chain_token = _call_chain.set(new_chain)
    extras_token = _extras.set(extras)
    # A run START (continues_chain is None onto an EMPTY prior chain) binds the
    # run-delivery context ONCE, and only when none is already ambient — so a
    # nested dispatch (non-empty prior chain), a SET continuation frame, and a
    # resume that already re-established the stored context all inherit rather
    # than re-mint. The address is the door's, read off the completion it bound
    # just before starting the run; a tool with no receiver reads ``None``.
    delivery_token: Token[RunDelivery | None] | None = None
    if continues_chain is None and not prior_chain and _run_delivery.get() is None:
        completion = get_park_completion()
        delivery = completion if completion[0] is not None else None
        delivery_token = _run_delivery.set(RunDelivery(run_delivery_id=str(uuid.uuid4()), delivery=delivery))
    try:
        yield
    finally:
        if delivery_token is not None:
            _run_delivery.reset(delivery_token)
        _extras.reset(extras_token)
        _call_chain.reset(chain_token)


__all__ = [
    "NestedDoorFrameError",
    "RunDelivery",
    "current_call_chain",
    "current_extras",
    "get_run_delivery",
    "get_run_delivery_id",
    "run_delivery",
    "tool_call_frame",
]
