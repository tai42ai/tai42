"""Typed, non-fatal run outcomes for a bounded structured-output loop and a recursion trip.

A structured-output ``response_format`` is enforced by the platform's own re-prompt loops,
on the tool plan and the native plan alike: the structured-output rail middleware
(:class:`~tai42_agents._internal.structured_rail.StructuredOutputRailMiddleware`) in a
graph, and :func:`~tai42_agents._internal.structured.ainvoke_structured` on the single-shot
door. A non-conforming answer is turned into a re-prompt message and the model is asked
again. Two mechanisms here make both stops explicit and readable:

* :func:`build_reprompt_handler` — the counting callable both loops call once per
  non-conforming answer; it returns the re-prompt message until the cap is reached, then
  raises :class:`RepromptCapError`; a graph's rails count per drive
  (:func:`drive_reprompt_scope`), so a compiled graph shared across runs gives each run the
  full cap;
* :func:`outcome_for_drive_error` — the map every face applies around its graph drive,
  turning :class:`RepromptCapError` and langgraph's ``GraphRecursionError`` into the
  terminal, non-fatal contract events the doors surface (and logging each once).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain.agents.factory import STRUCTURED_OUTPUT_ERROR_TEMPLATE
from langgraph.errors import GraphRecursionError
from tai42_contract.agent.events import RecursionLimitFinal, StreamEvent, StructuredOutputUnresolvedFinal
from tai42_contract.secrets import mask_secrets

logger = logging.getLogger(__name__)


class RepromptCapError(Exception):
    """The per-run structured-output re-prompt cap was reached with no conforming response.

    Carries the schema name, the number of non-conforming model responses, and the last
    error text (already masked). A face catches it around its graph drive and
    converts it to a :class:`StructuredOutputUnresolvedFinal`; it never surfaces as a raise.
    """

    def __init__(self, *, schema_name: str, attempts: int, error: str) -> None:
        """Carry the schema name, non-conforming-response count, and masked last error."""
        self.schema_name = schema_name
        self.attempts = attempts
        self.error = error
        super().__init__(f"structured-output re-prompt cap reached for {schema_name!r} after {attempts} attempts")


def _schema_name_of(exc: Exception, schema_name: str | None) -> str:
    """The requested schema's name for a cap outcome.

    The caller's ``schema_name`` (the requested schema) wins: a failure names the tool it
    concerns, which under a fan-out schema is one variant's tool, not the requested schema.
    With no ``schema_name``, the failure's tool names stand in: a
    :class:`~langchain.agents.structured_output.StructuredOutputValidationError` carries a
    single ``tool_name``; a ``MultipleStructuredOutputsError`` and a
    :class:`~tai42_agents._internal.structured_rail.StructuredOutputMissingError` carry
    ``tool_names``. A failure naming neither gets a neutral label.
    """
    if schema_name:
        return schema_name
    single = getattr(exc, "tool_name", None)
    if isinstance(single, str) and single:
        return single
    many = getattr(exc, "tool_names", None)
    if isinstance(many, list) and many:
        return ", ".join(str(name) for name in many)
    return "response_format"


class RepromptCounter:
    """The number of non-conforming structured responses one rail has seen in one drive."""

    def __init__(self) -> None:
        """Start at zero."""
        self.attempts = 0


_DRIVE_REPROMPT: ContextVar[dict[int, RepromptCounter]] = ContextVar("tai42_agents_drive_reprompt")


def drive_reprompt_counter(owner: object) -> RepromptCounter:
    """The bound drive's counter for ``owner`` (a rail), created on first use.

    Raises ``RuntimeError`` when no drive scope is bound: a compiled graph is shared across
    runs, so a counter outside a drive would be shared across them too.
    """
    try:
        counters = _DRIVE_REPROMPT.get()
    except LookupError:
        raise RuntimeError(
            "structured-output rail ran outside a drive scope: the code driving this agent graph must bind "
            "drive_reprompt_scope()"
        ) from None
    counter = counters.get(id(owner))
    if counter is None:
        counter = counters[id(owner)] = RepromptCounter()
    return counter


@contextmanager
def drive_reprompt_scope(counters: dict[int, RepromptCounter] | None = None) -> Iterator[dict[int, RepromptCounter]]:
    """Bind ``counters`` (a fresh map when ``None``) as the current drive's re-prompt counters.

    Every rail of the graph the drive runs counts on its own entry, so each drive gets every
    rail's full cap. A streaming drive passes the one map it owns into each per-step binding.
    """
    bound = counters if counters is not None else {}
    token = _DRIVE_REPROMPT.set(bound)
    try:
        yield bound
    finally:
        _DRIVE_REPROMPT.reset(token)


def build_reprompt_handler(
    cap: int, counter: Callable[[], RepromptCounter], *, schema_name: str | None = None
) -> Callable[[Exception], str]:
    """The structured-output loops' re-prompt callable: caps re-prompts at ``cap``, counting on ``counter()``.

    ``schema_name`` names the requested schema in the cap outcome; with none, the failure's tool names stand in.

    Each non-conforming structured-output response calls this once. While the count is within
    ``cap`` it returns the re-prompt message built from the error; on the response past the
    cap it raises :class:`RepromptCapError` carrying the masked last error (a parse or
    validation error, or the message naming the missing structured answer), ending the loop.
    """

    def handle(exc: Exception) -> str:
        state = counter()
        state.attempts += 1
        if state.attempts > cap:
            raise RepromptCapError(
                schema_name=_schema_name_of(exc, schema_name),
                attempts=state.attempts,
                error=str(mask_secrets(str(exc))),
            )
        return STRUCTURED_OUTPUT_ERROR_TEMPLATE.format(error=str(exc))

    return handle


def _recursion_limit_of(config: dict[str, Any] | None) -> int:
    """The super-step ceiling a run config pins, or langgraph's built-in default of 25."""
    if config is not None:
        limit = config.get("recursion_limit")
        if isinstance(limit, int):
            return limit
    return 25


def outcome_for_drive_error(exc: BaseException, config: dict[str, Any] | None) -> StreamEvent | None:
    """The terminal, non-fatal outcome event a caught graph-drive error maps to, else ``None``.

    :class:`RepromptCapError` → a :class:`StructuredOutputUnresolvedFinal`; langgraph's
    ``GraphRecursionError`` → a :class:`RecursionLimitFinal` naming the pinned ceiling. Each is
    logged once here. Any other error returns ``None`` so the caller re-raises it — the generic
    failure path is unchanged for everything else.
    """
    if isinstance(exc, RepromptCapError):
        logger.warning(
            "structured-output re-prompt cap reached for %r after %d attempts: %s",
            exc.schema_name,
            exc.attempts,
            exc.error,
        )
        return StructuredOutputUnresolvedFinal(schema_name=exc.schema_name, attempts=exc.attempts, error=exc.error)
    if isinstance(exc, GraphRecursionError):
        limit = _recursion_limit_of(config)
        logger.warning("agent run hit the recursion limit of %d super-steps: %s", limit, exc)
        return RecursionLimitFinal(limit=limit, steps=None)
    return None
