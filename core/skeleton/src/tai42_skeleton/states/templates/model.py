"""The validated platform state-template value object and its canonical wire projection.

Every section is a contract model (the single published source of its wire shape); the value
object is a frozen dataclass because its fragment field is literally named ``schema``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from tai42_contract.states.models import (
    StateRegimeRule,
    StateTemplateDeclarations,
    StateTemplateJq,
    StateTemplateParameter,
    StateTemplateReconcile,
    StateTemplateTrace,
)

TEMPLATE_KIND = "state-template"


@dataclass(frozen=True, slots=True)
class StateTemplate:
    """A validated platform state-template document.

    ``schema`` is the object-schema fragment (with ``$parameter`` markers); ``defaults`` are the
    parameter values applied when an attachment supplies none.
    """

    name: str
    description: str
    parameters: dict[str, StateTemplateParameter]
    schema: dict[str, Any]
    regimes: list[StateRegimeRule]
    declarations: StateTemplateDeclarations | None
    trace: StateTemplateTrace
    template_jq: dict[str, StateTemplateJq] = field(default_factory=dict)
    reconcile: StateTemplateReconcile | None = None

    def defaults(self) -> dict[str, Any]:
        """The parameter values applied when an attachment supplies none — only defaulted params."""
        return {name: copy.deepcopy(p.default) for name, p in self.parameters.items() if p.has_default}

    def to_document(self) -> dict[str, Any]:
        """The canonical JSON document for this template.

        The inverse of :func:`~tai42_skeleton.states.templates.validate.validate_template`,
        re-validatable and stable (the seed applier hashes it to tell a shipped default apart from
        an operator edit).
        """
        doc: dict[str, Any] = {"kind": TEMPLATE_KIND, "name": self.name, "description": self.description}
        if self.parameters:
            doc["parameters"] = {name: p.model_dump() for name, p in self.parameters.items()}
        doc["schema"] = self.schema
        if self.regimes:
            doc["regimes"] = [r.model_dump() for r in self.regimes]
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
        doc["trace"] = self.trace.model_dump()
        return doc


def _program_to_document(program: StateTemplateJq) -> dict[str, Any]:
    """The canonical JSON of one ``template_jq`` entry — purpose-specific keys only.

    The program body ``jq`` is emitted as its templated-text object (inline ``content`` or a
    stored ``id``), so a by-id reference round-trips unchanged.
    """
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
