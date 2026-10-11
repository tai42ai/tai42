"""The ``door="api"`` state context the direct tool/agent doors deposit.

The synchronous run-tool door, the background-submit run, the MCP ``tools/call`` edge and the
two agent-run SSE doors all start a run for a caller who named its own subject. Each deposits the
same ambient :class:`~tai42_contract.states.StateContext` — ``door="api"``, the caller as ``actor``,
and the named subject as the one resolvable candidate — so an async park of the run indexes under
that subject and a later run on the same subject finds and resumes it. Homing the deposit here keeps
the four doors from carrying per-door copies that drift.

A door with no named subject deposits nothing (a null context manager): the run still executes, but
an async park has no subject to index under.

The three live doors (the synchronous run-tool door, the MCP edge, the agent SSE doors) wrap their
run in :func:`live_door_scope` — the caller's execution identity, its run attribution, and the
subject context, in that order — and refuse an un-encodable failed outcome through
:func:`unencodable_outcome`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, contextmanager, nullcontext
from typing import TYPE_CHECKING, Any

from tai42_contract.states import StateContext, SubjectCandidates

from tai42_skeleton.states.context import state_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from contextlib import AbstractContextManager

    from tai42_contract.states import StateSubject

    from tai42_skeleton.tools.binding import ToolResultEncodingError

logger = logging.getLogger(__name__)


def api_state_context_value(subject: StateSubject, actor: str | None) -> StateContext:
    """The ``door="api"`` :class:`StateContext` for ``subject`` — the value the direct doors deposit.

    The single source of the shape: :func:`api_state_context` deposits it around a live run, and the
    background-submit door stores it on a crash-resume record so the re-drive replays under the same
    subject. Both reach it here, so the stored context can never drift from the deposited one.
    """
    return StateContext(
        door="api",
        candidates=SubjectCandidates(
            target_kind=subject.target_kind,
            target_name=subject.target_name,
            by_kind={subject.kind: subject.key},
        ),
        actor=actor,
    )


@contextmanager
def _deposited(subject: StateSubject, actor: str | None) -> Iterator[None]:
    with state_context(api_state_context_value(subject, actor)):
        yield


def api_state_context(subject: StateSubject | None, actor: str | None) -> AbstractContextManager[None]:
    """Deposit the ``door="api"`` :class:`StateContext` for ``subject`` around the wrapped run.

    ``actor`` is the caller's own principal (the accountable key, or ``None`` for an
    unauthenticated caller). With no ``subject`` this is a null context manager — the run
    executes with no ambient subject, so an async park is not subject-indexed.
    """
    if subject is None:
        return nullcontext()
    return _deposited(subject, actor)


@asynccontextmanager
async def caller_execution_identity(caller_key: str | None) -> AsyncIterator[None]:
    """Opportunistically bind ``caller_key``'s own execution identity for the wrapped direct-door run.

    A direct API door (the MCP ``tools/call`` edge, an agent-run SSE stream) carries no execution
    identity of its own, so a tool whose async ask parks could not rebind its continuation. This
    binds the caller's OWN key — the same live-grants rebuild the crash-resume re-drive uses — for
    the wrapped run and resets it after. It is NOT the door's authz gate (the route decision already
    ran): an identity already bound is never clobbered, an unauthenticated caller binds nothing, and
    a rebuild the infrastructure cannot answer degrades to the pre-bind behavior (unbound; the
    parking seam fail-closes loudly) with a warning rather than failing the call.
    """
    from tai42_skeleton.authz.execution_identity import (
        get_execution_identity,
        reset_execution_identity,
        set_execution_identity,
    )

    token = None
    if get_execution_identity() is None and caller_key is not None:
        from tai42_skeleton.authz.execution import rebuild_execution_identity

        try:
            identity = await rebuild_execution_identity(caller_key)
        except Exception:
            logger.warning("api door: could not rebuild the caller's execution identity", exc_info=True)
            identity = None
        if identity is not None:
            token = set_execution_identity(identity)
    try:
        yield
    finally:
        if token is not None:
            reset_execution_identity(token)


@asynccontextmanager
async def live_door_scope(
    subject: StateSubject | None, caller_key: str | None, *, attribute: bool
) -> AsyncIterator[None]:
    """The scope a live door runs its caller's tool or agent in.

    The caller's own execution identity (:func:`caller_execution_identity`), then — when
    ``attribute`` and a caller resolved — the caller as the run's attribution, so a runs-index row
    the dispatch registers is born with its ``user_id``, then the ``door="api"`` subject context
    (:func:`api_state_context`).
    """
    from tai42_contract.monitoring import RunAttribution

    from tai42_skeleton.tools.attribution import run_attribution

    async with caller_execution_identity(caller_key):
        attribution = (
            run_attribution(RunAttribution(user_id=caller_key))
            if attribute and caller_key is not None
            else nullcontext()
        )
        with attribution, api_state_context(subject, caller_key):
            yield


def unencodable_outcome(tool_name: str, outcome: Any) -> ToolResultEncodingError | None:
    """The refusal for a failed outcome no JSON encoder can render, or ``None`` when it encodes.

    A lone UTF-16 surrogate passes ``json.dumps`` but breaks the utf-8 wire encode; the refusal
    names the tool and the offending JSON path, and the finding is logged at WARNING. Each door
    raises it through its own error.
    """
    from tai42_skeleton.tools.binding import ToolResultEncodingError, find_lone_surrogate

    offending_path = find_lone_surrogate(outcome)
    if offending_path is None:
        return None
    logger.warning("tool %r failed with an outcome that cannot be JSON-encoded at %s", tool_name, offending_path)
    return ToolResultEncodingError(tool_name, offending_path)
