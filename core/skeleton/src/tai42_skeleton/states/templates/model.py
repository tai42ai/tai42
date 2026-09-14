"""The frozen value objects of a platform state-template document and their wire
projection.

The document's sub-shapes — its declarations, ``template_jq`` programs and ``reconcile``
programs — are the contract models (the single published source of their wire shape); the
remaining shapes stay frozen dataclasses because a pydantic model with a field literally
named ``schema`` shadows ``BaseModel.schema`` and warns, and the suite turns warnings into
errors (the contract models sidestep this with the ``schema_`` alias).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from tai42_contract.states.models import (
    StateTemplateDeclarations,
    StateTemplateJq,
    StateTemplateReconcile,
)

TEMPLATE_KIND = "state-template"


@dataclass(frozen=True, slots=True)
class TemplateParameter:
    """A fillable parameter: its value ``schema`` and an OPTIONAL ``default``. A
    parameter without a default must be referenced by a marker in the fragment and
    supplied at attach; ``has_default`` distinguishes an absent default from an explicit
    ``null`` default."""

    schema: dict[str, Any]
    has_default: bool = False
    default: Any = None


@dataclass(frozen=True, slots=True)
class RegimeRule:
    """One per-path writer rule: a template-relative ``path`` (object keys and the ``"*"``
    wildcard, which matches one list index or key) and its ``regime``
    (``single`` / ``composing`` / ``free``). An undeclared path is ``free``."""

    path: list[str]
    regime: str


@dataclass(frozen=True, slots=True)
class TemplateTrace:
    """The trace switch: when ``enabled``, the effective schema admits ``_trace`` and the
    platform ``apply`` chokepoint stamps it on every write under an attachment of this
    template."""

    enabled: bool = False


@dataclass(frozen=True, slots=True)
class StateTemplate:
    """A validated platform state-template document. ``schema`` is the object-schema
    fragment (with ``$parameter`` markers); ``defaults`` are the parameter values applied
    when an attachment supplies none."""

    name: str
    description: str
    parameters: dict[str, TemplateParameter]
    schema: dict[str, Any]
    regimes: list[RegimeRule]
    declarations: StateTemplateDeclarations | None
    trace: TemplateTrace
    template_jq: dict[str, StateTemplateJq] = field(default_factory=dict)
    reconcile: StateTemplateReconcile | None = None

    def defaults(self) -> dict[str, Any]:
        """The parameter values applied when an attachment supplies none — only defaulted
        params."""
        return {name: copy.deepcopy(p.default) for name, p in self.parameters.items() if p.has_default}

    def to_document(self) -> dict[str, Any]:
        """The canonical JSON document for this template — the inverse of
        :func:`~tai42_skeleton.states.templates.validate.validate_template`, re-validatable
        and stable (the seed applier hashes it to tell a shipped default apart from an
        operator edit)."""
        doc: dict[str, Any] = {"kind": TEMPLATE_KIND, "name": self.name, "description": self.description}
        if self.parameters:
            doc["parameters"] = {
                name: ({"schema": p.schema, "default": p.default} if p.has_default else {"schema": p.schema})
                for name, p in self.parameters.items()
            }
        doc["schema"] = self.schema
        if self.regimes:
            doc["regimes"] = [{"path": list(r.path), "regime": r.regime} for r in self.regimes]
        if self.declarations is not None:
            declarations: dict[str, Any] = {"schema": self.declarations.schema_}
            if self.declarations.check is not None:
                declarations["check"] = self.declarations.check.model_dump(exclude_none=True)
            doc["declarations"] = declarations
        if self.template_jq:
            doc["template_jq"] = {name: _program_to_document(program) for name, program in self.template_jq.items()}
        if self.reconcile is not None:
            doc["reconcile"] = {
                "orphans": self.reconcile.orphans.model_dump(exclude_none=True),
                "close": self.reconcile.close.model_dump(exclude_none=True),
                "resolutions": self.reconcile.resolutions.model_dump(exclude_none=True),
            }
        if self.trace.enabled:
            doc["trace"] = {"enabled": True}
        return doc


def _program_to_document(program: StateTemplateJq) -> dict[str, Any]:
    """The canonical JSON of one ``template_jq`` entry — purpose-specific keys only. The
    program body ``jq`` is emitted as its templated-text object (inline ``content`` or a stored
    ``id``), so a by-id reference round-trips unchanged."""
    if program.purpose == "input":
        return {
            "description": program.description,
            "purpose": "input",
            "params": list(program.params),
            "jq": program.jq.model_dump(exclude_none=True),
        }
    return {
        "description": program.description,
        "purpose": "update",
        "params": list(program.params),
        "reads": [list(p) for p in program.reads],
        "writes": [list(p) for p in program.writes],
        "jq": program.jq.model_dump(exclude_none=True),
    }
