"""The door contract: the parkable-door jq mixin and the pure result parsers.

A door that can drive a parkable run (a conversation route, a hook, a schedule) carries up to
four optional jq expressions, all on :class:`ParkableDoorMixin`:

* ``cancel_expr`` — names the parked interactions to cancel;
* ``resume_expr`` — names the parked interactions to resume with an answer, or to take a
  waiting outcome from;
* ``start_expr`` — builds the object dispatched as the started target's kwargs;
* ``extras_expr`` — builds the extras mapping handed to the started target.

Every one receives the run's currently parked interactions as the jq variable ``$parked`` beside
its own ``.`` input, so an author branches on what is already parked.

The two parsers turn a ``cancel_expr`` / ``resume_expr`` jq RESULT into the argument
:func:`~tai42_contract.interactions.visit.Visit` takes. They are PURE — no store read, no ambient
read — and raise :class:`DoorContractError` on any shape the contract does not accept, never a
silent default.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, cast

from pydantic import BaseModel, Field

from tai42_contract.errors import ErrorKind
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, TemplatedText, expression_annotation


class DoorContractError(ValueError):
    """A door-contract jq produced a result shape the contract does not accept.

    Raised by :func:`parse_cancel_result` / :func:`parse_resume_result` for anything outside the
    accepted shapes — a non-string id, a resume mapping missing ``id``/``payload`` or carrying
    an extra key, a blank id, or a scalar that is neither null nor a string. The author's
    expression is at fault, so it surfaces as bad input rather than being papered over.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


@dataclass(frozen=True)
class ResumeItem:
    """A resume of one parked interaction with an answer ``payload`` (a ``resume_expr`` ``{id, payload}``)."""

    id: str
    payload: Any


@dataclass(frozen=True)
class TakeItem:
    """A take of one parked interaction's waiting outcome (a ``resume_expr`` bare id)."""

    id: str


# The jq variable name every door-contract expression reads the run's parked interactions under,
# beside its own ``.`` input. Public so a door-contract evaluator declares/binds it by this name.
PARKED_VARIABLE = "parked"

# The declaration of that variable for a jq field annotation: its name, blurb and sample entry.
PARKED_VARIABLE_ANNOTATION = (
    PARKED_VARIABLE,
    "the run's currently parked interactions on the door's subject — the full entries "
    "list_parked() carries, each with its id, status, to, asked_by, question and answer-format fields",
    [
        {
            "id": "i-42",
            "status": "asking",
            "to": "caller",
            "asked_by": ["main"],
            "question": "proceed?",
            "answer_format": "confirm",
            "group_id": "g-1",
        }
    ],
)


class ParkableDoorMixin(BaseModel):
    """The four optional jq expressions a parkable-driving door carries.

    Each is an optional :class:`~tai42_contract.template.TemplatedText`. The concrete door refines
    the input-document gloss for its own ``.``; the accepted RESULT of each expression is fixed by
    the contract (the ``returns`` wording below and the two result parsers). Every expression reads
    the run's parked interactions as the jq variable ``$parked``.
    """

    cancel_expr: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="cancel expression",
                    blurb="the door's input document",
                    variables=[PARKED_VARIABLE_ANNOTATION],
                    returns="null (cancel nothing), a parked interaction id, or a list of ids to cancel",
                )
            }
        ),
    ] = None
    resume_expr: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="resume expression",
                    blurb="the door's input document",
                    variables=[PARKED_VARIABLE_ANNOTATION],
                    returns=(
                        "null (resume nothing), {id, payload} to resume an ask with an answer, a bare id to "
                        "take a waiting outcome, or a list of these"
                    ),
                )
            }
        ),
    ] = None
    start_expr: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="start expression",
                    blurb="the door's input document",
                    variables=[PARKED_VARIABLE_ANNOTATION],
                    returns="the object dispatched as the started target's kwargs; null starts nothing",
                )
            }
        ),
    ] = None
    extras_expr: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="extras expression",
                    blurb="the door's input document",
                    variables=[PARKED_VARIABLE_ANNOTATION],
                    returns="the extras mapping handed to the started target; null for no extras",
                )
            }
        ),
    ] = None


def _clean_id(value: Any) -> str:
    """Return ``value`` as a non-blank interaction id, or raise :class:`DoorContractError`."""
    if not isinstance(value, str) or not value.strip():
        raise DoorContractError(f"a door-contract id must be a non-empty string, got {value!r}")
    return value


def parse_cancel_result(result: Any) -> list[str]:
    """Turn a ``cancel_expr`` jq result into the interaction ids ``visit`` cancels.

    Accepts ``null`` (nothing), a single id string, or a list of id strings. Any other shape — a
    number, a mapping, a list holding a non-string, a blank id — raises :class:`DoorContractError`.
    """
    if result is None:
        return []
    if isinstance(result, str):
        return [_clean_id(result)]
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        return [_clean_id(item) for item in cast("Sequence[Any]", result)]
    raise DoorContractError(
        f"cancel_expr must yield null, an id string, or a list of id strings, got {type(result).__name__}"
    )


def _parse_resume_item(item: Any) -> ResumeItem | TakeItem:
    """Turn one ``resume_expr`` element into a :class:`ResumeItem` (``{id, payload}``) or :class:`TakeItem` (id)."""
    if isinstance(item, str):
        return TakeItem(id=_clean_id(item))
    if isinstance(item, Mapping):
        mapping = cast("Mapping[str, Any]", item)
        extra = set(mapping) - {"id", "payload"}
        if extra:
            raise DoorContractError(f"a resume mapping carries only {{id, payload}}, got extra keys {sorted(extra)}")
        if "payload" not in mapping:
            raise DoorContractError("a resume mapping needs a 'payload' (a bare id is a take, not a mapping)")
        return ResumeItem(id=_clean_id(mapping.get("id")), payload=mapping["payload"])
    raise DoorContractError(
        f"a resume item must be an id string (a take) or {{id, payload}} (a resume), got {type(item).__name__}"
    )


def parse_resume_result(result: Any) -> list[ResumeItem | TakeItem]:
    """Turn a ``resume_expr`` jq result into the resume/take items ``visit`` acts on.

    Accepts ``null`` (nothing), a single ``{id, payload}`` mapping (a resume), a single id string
    (a take), or a list mixing the two. Any other shape raises :class:`DoorContractError`.
    """
    if result is None:
        return []
    if isinstance(result, str):
        return [_parse_resume_item(result)]
    if isinstance(result, Mapping):
        return [_parse_resume_item(result)]
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        return [_parse_resume_item(item) for item in cast("Sequence[Any]", result)]
    raise DoorContractError(
        f"resume_expr must yield null, {{id, payload}}, an id string, or a list of these, got {type(result).__name__}"
    )


__all__ = [
    "PARKED_VARIABLE",
    "PARKED_VARIABLE_ANNOTATION",
    "DoorContractError",
    "ParkableDoorMixin",
    "ResumeItem",
    "TakeItem",
    "parse_cancel_result",
    "parse_resume_result",
]
