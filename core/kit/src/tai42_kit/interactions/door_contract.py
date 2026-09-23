"""Evaluate a door's :class:`~tai42_contract.interactions.door_contract.ParkableDoorMixin` jqs.

A parkable-driving door (a conversation route, a hook, a schedule) carries up to four jq
expressions. :func:`evaluate_door_contract` renders each declared one, runs it over the door's own
input document with the run's parked interactions bound as ``$parked``, and parses the results into
the values a door hands :func:`tai42_app.interactions.visit`:

* ``cancel`` — the parked ids to cancel;
* ``resume`` — the resume/take items;
* ``extras`` — the started run's extras mapping;
* ``start`` — :data:`DOOR_START_DEFAULT` when the door declares no ``start_expr`` (the door supplies
  its own default kwargs), ``None`` when a declared ``start_expr`` yielded null (start nothing), or
  the object the ``start_expr`` yielded (the start input).

Homed in kit so a backend plugin, a hook, a schedule and a conversation route all reach it without
importing the skeleton (kit cannot import the skeleton). It is a PURE function of its arguments with
respect to the interaction store and the ambient state context: the ``parked`` list is INJECTED by
the caller (each door fetches it once for its own subject), and nothing here reads the store or the
current state context — only the declared jqs render and evaluate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from tai42_contract.interactions import ParkedEntry
from tai42_contract.interactions.door_contract import (
    PARKED_VARIABLE,
    DoorContractError,
    ParkableDoorMixin,
    ResumeItem,
    TakeItem,
    parse_cancel_result,
    parse_resume_result,
)
from tai42_contract.template import TemplatedText

from tai42_kit.utils.data import run_jq_bounded
from tai42_kit.utils.render import render_templated_text


class _DoorStartDefault:
    """The type of :data:`DOOR_START_DEFAULT` — a door with no declared ``start_expr``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "DOOR_START_DEFAULT"


# Sentinel: the door declared no ``start_expr``, so it starts its OWN default payload rather than a
# contract-built one. Distinct from ``None`` (a declared ``start_expr`` yielded null — start nothing).
DOOR_START_DEFAULT = _DoorStartDefault()


@dataclass(frozen=True)
class DoorContractOutcome:
    """The resolved :func:`~tai42_skeleton.interactions.visit.visit` arguments a door contract yields.

    ``start`` is :data:`DOOR_START_DEFAULT` (no ``start_expr`` declared), ``None`` (declared
    ``start_expr`` yielded null → start nothing), or the ``dict`` the ``start_expr`` built.
    """

    cancel: list[str]
    resume: list[ResumeItem | TakeItem]
    extras: dict[str, Any]
    start: _DoorStartDefault | dict[str, Any] | None


async def evaluate_door_contract(
    contract: ParkableDoorMixin, jq_input: Any, parked: list[dict[str, Any]]
) -> DoorContractOutcome:
    """Render + evaluate ``contract``'s jqs over ``jq_input`` with ``parked`` bound as ``$parked``.

    ``parked`` is the run's currently parked interactions as plain jq documents (the caller dumps its
    :class:`~tai42_contract.interactions.ParkedEntry` list). Returns the :class:`DoorContractOutcome`
    a door hands ``visit``. Any jq result outside the accepted shapes raises
    :class:`~tai42_contract.interactions.door_contract.DoorContractError` (never a silent default);
    an undeclared ``$name``, a multi-value emit, or a timeout surfaces loudly.
    """
    variables = {PARKED_VARIABLE: parked}

    cancel = parse_cancel_result(await _eval("cancel_expr", contract.cancel_expr, jq_input, variables))
    resume = parse_resume_result(await _eval("resume_expr", contract.resume_expr, jq_input, variables))

    start: _DoorStartDefault | dict[str, Any] | None
    if contract.start_expr is None:
        start = DOOR_START_DEFAULT
    else:
        start = _parse_object("start_expr", await _eval("start_expr", contract.start_expr, jq_input, variables))

    extras = _parse_object("extras_expr", await _eval("extras_expr", contract.extras_expr, jq_input, variables)) or {}

    return DoorContractOutcome(cancel=cancel, resume=resume, extras=extras, start=start)


def parked_entries_for_jq(entries: Iterable[ParkedEntry]) -> list[dict[str, Any]]:
    """Dump parked/ask entries to the plain jq documents a door binds as ``$parked`` / ``$asks``.

    Each entry is dumped compact (``exclude_none=True``) so an unset optional field is absent rather
    than a null key — the single shape every door presents, matching the documented sample entry.
    """
    return [entry.model_dump(mode="json", exclude_none=True) for entry in entries]


async def _eval(field: str, text: TemplatedText | None, jq_input: Any, variables: dict[str, Any]) -> Any:
    """Render ``text`` to its jq program and evaluate it over ``jq_input``; ``None`` when unset.

    An unset expression yields ``None`` (the parsers read that as "nothing"). An empty pipeline
    yields ``None`` too — a door contract's absent action is a null result, never an error — while a
    genuine evaluation error (a bad program, an undeclared variable, a timeout) raises loudly. A
    program that emits MORE than one value is the author's error and raises :class:`DoorContractError`
    (a door action is one value, never a stream).
    """
    if text is None:
        return None
    rendered = await render_templated_text(text)
    if not rendered:
        return None
    emitted = await run_jq_bounded(rendered, jq_input, 1, variables=variables)
    if len(emitted) > 1:
        raise DoorContractError(f"{field} must emit a single value, but its jq emitted more than one")
    return emitted[0] if emitted else None


def _parse_object(field: str, result: Any) -> dict[str, Any] | None:
    """A ``start_expr`` / ``extras_expr`` jq result as an object, ``None`` for null, else raise.

    Both expressions build a kwargs/extras MAPPING; null means "nothing" (start nothing / no extras).
    Any other shape is the author's error and raises :class:`DoorContractError`, never a silent
    default.
    """
    if result is None:
        return None
    if isinstance(result, Mapping):
        return dict(cast("Mapping[str, Any]", result))
    raise DoorContractError(f"{field} must yield an object or null, got {type(result).__name__}")


__all__ = ["DOOR_START_DEFAULT", "DoorContractOutcome", "evaluate_door_contract", "parked_entries_for_jq"]
