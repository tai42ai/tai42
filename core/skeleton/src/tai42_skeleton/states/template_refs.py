"""The stored template ids a state declaration and a state template document name, at their sites.

The state write doors check these before anything renders, and the template delete doors
read the same lists to find what still names an id.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.states.models import StateDeclaration, StateTemplateDocument
from tai42_contract.template import TemplatedText
from tai42_contract.tools import TemplateRef


def _ref(text: Any, site: str) -> list[TemplateRef]:
    if isinstance(text, TemplatedText) and text.id is not None:
        return [TemplateRef(id=text.id, site=site)]
    return []


def declaration_template_refs(decl: StateDeclaration) -> list[TemplateRef]:
    """The declaration's base ``schema`` when it is given by stored id (site ``schema``)."""
    return _ref(decl.schema_, "schema")


def state_template_template_refs(doc: StateTemplateDocument) -> list[TemplateRef]:
    """Every slot of ``doc`` given by stored id, in document order.

    Sites: ``schema``, ``declarations.check``, ``template_jq.<name>.jq`` and
    ``reconcile.orphans|close|resolutions``.
    """
    refs = _ref(doc.schema_, "schema")
    if doc.declarations is not None:
        refs += _ref(doc.declarations.check, "declarations.check")
    for name, program in (doc.template_jq or {}).items():
        refs += _ref(program.jq, f"template_jq.{name}.jq")
    if doc.reconcile is not None:
        for slot in ("orphans", "close", "resolutions"):
            refs += _ref(getattr(doc.reconcile, slot), f"reconcile.{slot}")
    return refs


__all__ = ["declaration_template_refs", "state_template_template_refs"]
