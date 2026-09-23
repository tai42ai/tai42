"""The platform state-template document model, its validation, and the compose machinery.

A state TEMPLATE is ONE JSON document an operator attaches to a state: a schema fragment
with fillable parameters, per-path writer regimes, attach-time declarations, and a trace
switch. Attaching a template to a state places its fragment at a path once; this package
owns the document SHAPE and the pure transforms the attach and the effective-schema composer
lean on. The document holds only what the platform owns; a consumer keeps its own documents
beside the template under its own kind (validated through its registered attach validator),
and any key outside the platform set is refused.

Three pure entry points carry the feature:

- :func:`~tai42_skeleton.states.templates.validate.validate_template` parses and checks a raw
  document against every platform rule, raising
  :class:`~tai42_contract.states.errors.TemplateValidationError` loudly on the first violation.
- :func:`~tai42_skeleton.states.templates.parameters.substitute_parameters` replaces
  ``{"$parameter": "<name>"}`` markers in a fragment with supplied values.
- :func:`~tai42_skeleton.states.templates.compose.compose_effective_schema` places each
  attachment's substituted (and, when the template traces, ``_trace``-stamped) fragment into a
  base schema, refusing collisions.
"""

from __future__ import annotations

from tai42_skeleton.states.templates.compose import compose_effective_schema
from tai42_skeleton.states.templates.jq import MEMBER_JQ_VARIABLES, template_jq_prelude
from tai42_skeleton.states.templates.model import (
    TEMPLATE_KIND,
    RegimeRule,
    StateTemplate,
    TemplateParameter,
    TemplateTrace,
)
from tai42_skeleton.states.templates.parameters import substitute_parameters
from tai42_skeleton.states.templates.regimes import REGIMES, path_overlaps, regime_for
from tai42_skeleton.states.templates.validate import (
    DECLARATIONS_CHECK_VARIABLES,
    RECONCILE_JQ_VARIABLES,
    validate_template,
)

__all__ = [
    "DECLARATIONS_CHECK_VARIABLES",
    "MEMBER_JQ_VARIABLES",
    "RECONCILE_JQ_VARIABLES",
    "REGIMES",
    "TEMPLATE_KIND",
    "RegimeRule",
    "StateTemplate",
    "TemplateParameter",
    "TemplateTrace",
    "compose_effective_schema",
    "path_overlaps",
    "regime_for",
    "substitute_parameters",
    "template_jq_prelude",
    "validate_template",
]
