"""The templated-text value type and the render mixins that carry it.

:class:`TemplatedText` is the ONE shape every renderable text on the wire takes: inline
``content`` or a stored ``id``, plus the ``kwargs`` its render takes. ``ConditionMixin``
/ ``ExprMixin`` carry the templated text a schema needs to render a condition or an
expression. The ``rendered_condition`` / ``rendered_expr`` methods are impl (they reach
the live ``resource_manager``) and live with the ``ResourceManager`` impl, not in this
pure-model contract — only the field shape is the contract.

Two vendor annotations declare themselves in every generated JSON schema, both schema
METADATA only (an ``x-``-prefixed key is ignored by JSON-Schema validation, so neither
can change what values a schema accepts):

* :data:`TEMPLATED_TEXT_ANNOTATION_KEY` on the :class:`TemplatedText` TYPE, so a schema
  consumer recognizes every property of this type without matching field names.
* :data:`EXPRESSION_ANNOTATION_KEY` on each jq-typed property, built by
  :func:`expression_annotation`, so a consumer (an OpenAPI reader, a tool-listing
  client, editor tooling) learns the expression's input/output without guessing. A
  declaring model refines the generic wording here by overriding the field with its own
  surface-specific payload (see the backend callback schema).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

# The vendor-extension key a jq-typed property carries in generated JSON schemas.
EXPRESSION_ANNOTATION_KEY = "x-tai42-expression"

# The vendor-extension key the ``TemplatedText`` type stamps on its own generated JSON
# schema, and its payload: the language the text is rendered in.
TEMPLATED_TEXT_ANNOTATION_KEY = "x-tai42-templated-text"
TEMPLATED_TEXT_ANNOTATION: dict[str, Any] = {"language": "jinja"}

# Sentinel distinguishing "no sample supplied" from a legitimate sample value of
# None / {} / [] (all of which are meaningful sample documents).
_UNSET: Any = object()


def expression_annotation(
    *,
    label: str | None = None,
    blurb: str | None = None,
    keys: Sequence[tuple[str, str]] | None = None,
    returns: str | None = None,
    caveats: Sequence[str] | None = None,
    sample: Any = _UNSET,
) -> dict[str, Any]:
    """Build the :data:`EXPRESSION_ANNOTATION_KEY` payload for one jq-typed property.

    The payload is plain JSON-serializable data. ``language`` is always present
    (fixed to ``"jq"``); every other entry is emitted only when supplied — an
    omitted argument leaves its key absent rather than ``None``-filled.

    * ``label`` — a short human name for the field.
    * ``blurb`` — what the expression's INPUT document is.
    * ``keys`` — ``(name, gloss)`` pairs glossing the input document's known
      top-level keys. An EMPTY sequence is meaningful — it states the input is
      untyped/free-form — and is emitted as ``[]``, distinct from omitting the
      argument (shape unknown/undeclared).
    * ``returns`` — what the expression's result is consumed as.
    * ``caveats`` — evaluation edge cases a caller should know.
    * ``sample`` — a representative input document (any JSON value; ``None`` and
      ``{}`` are valid samples, so absence is a distinct default).
    """
    payload: dict[str, Any] = {"language": "jq"}
    if label is not None:
        payload["label"] = label
    if blurb is not None:
        payload["blurb"] = blurb
    if keys is not None:
        payload["keys"] = [{"name": name, "gloss": gloss} for name, gloss in keys]
    if returns is not None:
        payload["returns"] = returns
    if caveats is not None:
        payload["caveats"] = list(caveats)
    if sample is not _UNSET:
        payload["sample"] = sample
    return payload


class TemplatedText(BaseModel):
    """A renderable text: its source plus the parameters its render takes.

    EXACTLY ONE source is set — ``content`` (the text inline) or ``id`` (the id of a
    stored resource holding the text); neither and both are refused at construction.
    ``kwargs`` are the render parameters for the TEXT and apply either way, so moving a
    text from inline to stored (or back) leaves its parameters untouched.
    """

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={TEMPLATED_TEXT_ANNOTATION_KEY: TEMPLATED_TEXT_ANNOTATION}
    )

    content: str | None = None
    id: str | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_exactly_one_source(self) -> Self:
        if self.content is not None and self.id is not None:
            raise ValueError(
                f"a templated text takes 'content' or 'id', not both: content={self.content!r}, id={self.id!r}"
            )
        if self.content is None and self.id is None:
            raise ValueError("a templated text takes inline 'content' or a stored 'id'; neither was supplied")
        return self

    @model_serializer(mode="wrap")
    def _serialize_canonical(self, handler: Any) -> dict[str, Any]:
        """The single wire shape every door emits: exactly the ONE source key that is set
        (``content`` or ``id``) and ``kwargs`` only when it carries render parameters. The
        unset source key and an empty ``kwargs`` are dropped, so the wire never carries a
        null source or empty noise regardless of the caller's dump flags — a strict consumer
        sees exactly one source and no nulls."""
        data = handler(self)
        data.pop("content" if self.content is None else "id", None)
        if not self.kwargs:
            data.pop("kwargs", None)
        return data


class ConditionMixin(BaseModel):
    # The generic payload is the TRUTHY common denominator: a non-overriding
    # inheriting surface (hook registration) evaluates ``condition`` over its own
    # input document and proceeds on a truthy result. It is NOT the universal
    # truth — the access-control policy/role require EXACTLY boolean ``true`` (any
    # other truthy value DENIES), so they OVERRIDE with a strict-true payload; the
    # backend callback overrides to pin its own input document. A surface whose
    # facts are sharper than "truthy proceeds" must override rather than inherit
    # this wording (see the access-control and backend callback schemas).
    condition: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="condition",
                    blurb="the declaring surface's input document",
                    returns="truthy to proceed; a falsy result gates the surface's action off",
                )
            }
        ),
    ] = None


class ExprMixin(BaseModel):
    # Same generic-honesty rule as ``ConditionMixin.condition``: every inheriting
    # surface evaluates ``expr`` over its own input document and consumes the
    # transformed result; surfaces with sharper facts override the field.
    expr: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="expression",
                    blurb="the declaring surface's input document",
                    returns="the transformed value the declaring surface consumes",
                )
            }
        ),
    ] = None


__all__ = [
    "EXPRESSION_ANNOTATION_KEY",
    "TEMPLATED_TEXT_ANNOTATION",
    "TEMPLATED_TEXT_ANNOTATION_KEY",
    "ConditionMixin",
    "ExprMixin",
    "TemplatedText",
    "expression_annotation",
]
