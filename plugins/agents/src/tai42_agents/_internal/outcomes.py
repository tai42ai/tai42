"""Typed, non-fatal run outcomes for a bounded structured-output loop and a recursion trip.

A structured-output ``response_format`` is enforced through langchain's tool-calling
retry rail: a non-conforming model response is turned into an error tool message and the
model is re-prompted. The rail has no counter of its own, so the only bound is the graph
``recursion_limit`` — a runaway that re-prompts until the super-step budget is spent, then
fails generically. Two mechanisms here make both stops explicit and readable:

* :func:`build_reprompt_handler` — a per-run counting ``ToolStrategy.handle_errors``
  callable that returns the same re-prompt message the default rail does until the cap is
  reached, then raises :class:`RepromptCapError`;
* :func:`outcome_for_drive_error` — the map every face applies around its graph drive,
  turning :class:`RepromptCapError` and langgraph's ``GraphRecursionError`` into the
  terminal, non-fatal contract events the doors surface (and logging each once).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents.factory import STRUCTURED_OUTPUT_ERROR_TEMPLATE
from langgraph.errors import GraphRecursionError
from tai42_contract.agent.events import RecursionLimitFinal, StreamEvent, StructuredOutputUnresolvedFinal
from tai42_contract.secrets import mask_secrets

logger = logging.getLogger(__name__)


class RepromptCapError(Exception):
    """The per-run structured-output re-prompt cap was reached with no conforming response.

    Carries the schema name, the number of non-conforming model responses, and the last
    validation error text (already masked). A face catches it around its graph drive and
    converts it to a :class:`StructuredOutputUnresolvedFinal`; it never surfaces as a raise.
    """

    def __init__(self, *, schema_name: str, attempts: int, error: str) -> None:
        """Carry the schema name, non-conforming-response count, and masked last error."""
        self.schema_name = schema_name
        self.attempts = attempts
        self.error = error
        super().__init__(f"structured-output re-prompt cap reached for {schema_name!r} after {attempts} attempts")


def _schema_name_of(exc: Exception) -> str:
    """The structured-output schema name a langchain structured-output error names.

    A :class:`~langchain.agents.structured_output.StructuredOutputValidationError` carries a
    single ``tool_name``; a ``MultipleStructuredOutputsError`` carries ``tool_names``. Falls
    back to a neutral label when neither is present.
    """
    single = getattr(exc, "tool_name", None)
    if isinstance(single, str) and single:
        return single
    many = getattr(exc, "tool_names", None)
    if isinstance(many, list) and many:
        return ", ".join(str(name) for name in many)
    return "response_format"


def build_reprompt_handler(cap: int) -> Callable[[Exception], str]:
    """A fresh per-run ``ToolStrategy.handle_errors`` callable that caps re-prompts at ``cap``.

    Each non-conforming structured-output response calls this once. While the count is within
    ``cap`` it returns the SAME re-prompt message the default rail returns (so a capped run
    re-prompts identically up to the bound); on the response past the cap it raises
    :class:`RepromptCapError` carrying the masked last validation error, ending the loop.
    The counter lives in this closure, so it is scoped to the one run whose strategy holds it.
    """
    state = {"attempts": 0}

    def handle(exc: Exception) -> str:
        state["attempts"] += 1
        if state["attempts"] > cap:
            raise RepromptCapError(
                schema_name=_schema_name_of(exc),
                attempts=state["attempts"],
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
