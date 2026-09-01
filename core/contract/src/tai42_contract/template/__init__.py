"""Render-mixin models.

``ConditionMixin`` / ``ExprMixin`` carry the jq/template fields a schema needs to
render a condition or an expression. The ``rendered_condition`` / ``rendered_expr``
methods are impl (they reach the live ``resource_manager``) and live with the
``ResourceManager`` impl, not in this pure-model contract — only the field shape
is the contract.

The jq-typed STRING fields (``condition`` / ``expr``) additionally declare
themselves in every generated JSON schema: each carries a vendor annotation under
:data:`EXPRESSION_ANNOTATION_KEY` built by :func:`expression_annotation`, so any
schema consumer (an OpenAPI reader, a tool-listing client, editor tooling) can
recognize the property as a jq expression and learn its input/output without
guessing from field names. The annotation is schema METADATA only — it never
validates a value — and it is strictly additive: a field without one generates a
byte-identical schema to a plain declaration. A declaring model refines the
generic wording here by overriding the field with its own surface-specific
payload (see the backend callback schema). The ``*_id`` / ``*_kwargs`` template
companions are NOT jq strings and stay unannotated.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import BaseModel, Field

# The vendor-extension key a jq-typed string property carries in generated JSON
# schemas. An ``x-``-prefixed key is ignored by JSON-Schema validation, so the
# annotation can never change what values a schema accepts.
EXPRESSION_ANNOTATION_KEY = "x-tai42-expression"

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
        str | None,
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
    condition_id: str | None = None
    condition_kwargs: dict[str, Any] | None = None


class ExprMixin(BaseModel):
    # Same generic-honesty rule as ``ConditionMixin.condition``: every inheriting
    # surface evaluates ``expr`` over its own input document and consumes the
    # transformed result; surfaces with sharper facts override the field.
    expr: Annotated[
        str | None,
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
    expr_id: str | None = None
    expr_kwargs: dict[str, Any] | None = None


__all__ = ["EXPRESSION_ANNOTATION_KEY", "ConditionMixin", "ExprMixin", "expression_annotation"]
