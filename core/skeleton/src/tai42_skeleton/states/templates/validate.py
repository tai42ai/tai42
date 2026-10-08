"""The platform state-template document parser and validator.

Parses a raw JSON document into a :class:`~tai42_skeleton.states.templates.model.StateTemplate`:
the document's shape (every section, the name law, no key outside the platform set) is the
contract's :class:`~tai42_contract.states.models.StateTemplateDocument`; this module adds the
rules that span sections or need jq — parameter markers against declared parameters, the
fragment as an object schema, regime and program paths inside the fragment, program names and
params as jq identifiers, and every inline jq body compiling. A consumer keeps its own documents
beside the template under its own kind (validated through its registered attach validator).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError
from tai42_contract.states.errors import SchemaValidationError, TemplateValidationError
from tai42_contract.states.models import (
    StateTemplateDeclarations,
    StateTemplateDocument,
    StateTemplateJq,
    StateTemplateParameter,
    StateTemplateReconcile,
)
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.jq_util import compile_check

from tai42_skeleton.states.schema import _validate_schema
from tai42_skeleton.states.templates.jq import _compile_template_jq_inline
from tai42_skeleton.states.templates.model import TEMPLATE_KIND, StateTemplate
from tai42_skeleton.states.templates.parameters import _iter_marker_names, substitute_parameters
from tai42_skeleton.states.templates.regimes import _validate_regime_path

# The named jq variables a declarations ``check`` may reference: ``$parameters`` — the
# attachment's effective parameters (defaults overlaid by supplied values), bound at the
# attach seam that evaluates the check. Declared here so an author's check compiles at
# upload and every evaluator binds the same set.
DECLARATIONS_CHECK_VARIABLES: tuple[str, ...] = ("parameters",)

# The named jq variables each reconcile program reads beside its ``.``, per program. ``.``
# holds the one thing the program maps (``orphans``/``close`` the record subtree,
# ``resolutions`` the new declarations); everything else the reconciler supplies is a named
# variable. Declared here so a program compiles at upload and the reconcile evaluator binds
# the same set for each label.
RECONCILE_JQ_VARIABLES: dict[str, tuple[str, ...]] = {
    "orphans": ("previous", "new"),
    "close": ("id", "resolution"),
    "resolutions": (),
}

#: A ``template_jq`` program name: a lowercase identifier — the readable handle an
#: ``input`` program is called by (``<name>(…)``), and a jq-safe function name.
_MEMBER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _require_type(value: Any, kind: type, *, where: str) -> Any:
    if isinstance(value, bool) or not isinstance(value, kind):
        raise TemplateValidationError(f"{where} must be a {kind.__name__}, got {type(value).__name__}")
    return value


def _compile_check_jq(expr: str, *, where: str) -> None:
    """Compile-check the declarations ``check`` predicate.

    Declares the named variables the attach seam binds at evaluation (``$parameters`` — the
    effective attach parameters) so an author may reference them; a failure is a loud template error.
    """
    try:
        compile_check(expr, variables=DECLARATIONS_CHECK_VARIABLES)
    except Exception as exc:
        raise TemplateValidationError(f"{where} is not a valid jq expression: {exc}") from exc


def _check_declarations(declarations: StateTemplateDeclarations) -> None:
    """An inline ``check`` is a non-empty jq predicate that compiles.

    A by-id check is rendered and compiled at the save door, which can fetch the stored resource.
    """
    check = declarations.check
    if check is None or check.content is None:
        return
    if not check.content.strip():
        raise TemplateValidationError("declarations check must be a non-empty jq predicate or omitted")
    _compile_check_jq(check.content, where="declarations check")


def _check_identifier_list(values: list[str], *, where: str) -> None:
    """A list of unique identifier strings (a program's ``params``)."""
    seen: set[str] = set()
    for item in values:
        if not _MEMBER_NAME_RE.fullmatch(item):
            raise TemplateValidationError(
                f"{where} entry {item!r} must be an identifier matching {_MEMBER_NAME_RE.pattern}"
            )
        if item in seen:
            raise TemplateValidationError(f"{where} names {item!r} more than once")
        seen.add(item)


def _check_path_list(paths: list[list[str]], *, where: str) -> None:
    """Each template-relative record path is a list of non-empty segments (keys or the ``"*"`` wildcard)."""
    for i, path in enumerate(paths):
        for seg in path:
            if not seg:
                raise TemplateValidationError(f"{where}[{i}] segment {seg!r} must be a non-empty string ('*' or a key)")


def _check_program_body(text: TemplatedText, *, where: str) -> None:
    """An inline program body is non-empty; a by-id body is checked when the save door renders it."""
    if text.content is not None and not text.content.strip():
        raise TemplateValidationError(f"{where} must be a non-empty jq program")


def _check_template_jq(programs: dict[str, StateTemplateJq]) -> None:
    """Each program name and its ``params`` are jq identifiers, each body non-empty, each path well formed.

    An all-inline section then compiles over the sibling INPUT-purpose prelude (so any program may
    call an input program as ``tjq_<name>({…})``); a by-id body defers the section's compile to the
    save door, which can render the stored resources.
    """
    for name, program in programs.items():
        where = f"template_jq {name!r}"
        if not _MEMBER_NAME_RE.fullmatch(name):
            raise TemplateValidationError(f"template_jq name {name!r} must match {_MEMBER_NAME_RE.pattern}")
        _check_program_body(program.jq, where=f"{where} jq")
        _check_identifier_list(program.params, where=f"{where} params")
        _check_path_list(program.reads, where=f"{where} reads")
        _check_path_list(program.writes, where=f"{where} writes")
    _compile_template_jq_inline(programs)


def _check_reconcile(reconcile: StateTemplateReconcile) -> None:
    """Each inline reconcile program is non-empty and compiles with its label's variables bound.

    A by-id body defers its compile to the save door
    (:meth:`~tai42_skeleton.states.service.StatesService._compile_by_id_reconcile`), the point that
    can render the stored resource.
    """
    for label in ("orphans", "close", "resolutions"):
        text: TemplatedText = getattr(reconcile, label)
        _check_program_body(text, where=f"reconcile {label}")
        if text.content is not None:
            try:
                compile_check(text.content, variables=RECONCILE_JQ_VARIABLES[label])
            except Exception as exc:
                raise TemplateValidationError(f"reconcile {label} is not a valid jq expression: {exc}") from exc


def _validate_fragment_schema(name: str, fragment: dict[str, Any]) -> None:
    """The fragment, with defaults substituted, must pass the shared object-schema validator.

    Object-rooted, ≥1 property, a valid draft 2020-12 schema.
    """
    try:
        _validate_schema(fragment)
    except SchemaValidationError as exc:
        raise TemplateValidationError(f"template {name!r} schema fragment is not a valid object schema: {exc}") from exc


def _defaults_fragment(
    name: str, schema: dict[str, Any], parameters: dict[str, StateTemplateParameter]
) -> dict[str, Any]:
    """The fragment with parameter defaults substituted, after the marker/parameter cross-check.

    Every marker names a declared parameter, a no-default parameter appears as a marker, and the
    substituted fragment is a valid object schema.
    """
    marker_names = set(_iter_marker_names(schema))
    unknown = sorted(marker_names - set(parameters))
    if unknown:
        raise TemplateValidationError(f"schema references undeclared parameter(s) {unknown}")
    for pname, param in parameters.items():
        if not param.has_default and pname not in marker_names:
            raise TemplateValidationError(
                f"parameter {pname!r} has no default, so it must appear as a $parameter marker in the fragment"
            )
    fragment = substitute_parameters(schema, {n: p.default for n, p in parameters.items() if p.has_default})
    _validate_fragment_schema(name, fragment)
    return fragment


def _check_program_paths(programs: dict[str, StateTemplateJq], fragment: dict[str, Any]) -> None:
    """Every update program's ``reads``/``writes`` path lies inside the fragment."""
    for program_name, program in programs.items():
        for path in (*program.reads, *program.writes):
            try:
                _validate_regime_path(fragment, path)
            except TemplateValidationError as exc:
                raise TemplateValidationError(f"template_jq {program_name!r}: {exc}") from exc


def validate_template(doc: Any) -> StateTemplate:
    """Parse and validate a raw platform state-template document into a :class:`StateTemplate`.

    The document must carry ``kind == "state-template"`` and a dict fragment ``schema`` (a by-id
    fragment is resolved by the caller first) and parse as a
    :class:`~tai42_contract.states.models.StateTemplateDocument`; then a no-default parameter
    must appear as a marker and every marker names a declared parameter, the defaults-substituted
    fragment passes ``_validate_schema``, regime and update-program paths lie inside the fragment
    (``"*"`` only over ``items``/``additionalProperties``), program names and params are jq
    identifiers, and every inline jq body compiles (a by-id body is rendered and compiled at the
    save door). Raises :class:`~tai42_contract.states.errors.TemplateValidationError` on the first
    violation.
    """
    _require_type(doc, dict, where="template document")
    if doc.get("kind") != TEMPLATE_KIND:
        raise TemplateValidationError(f"template kind must be {TEMPLATE_KIND!r}, got {doc.get('kind')!r}")
    schema = _require_type(doc.get("schema"), dict, where="schema")
    try:
        typed = StateTemplateDocument.model_validate(doc)
    except ValidationError as exc:
        raise TemplateValidationError(str(exc)) from exc
    name = typed.name
    parameters = typed.parameters
    defaults_fragment = _defaults_fragment(name, schema, parameters)

    for rule in typed.regimes:
        _validate_regime_path(defaults_fragment, list(rule.path))

    if typed.declarations is not None:
        _check_declarations(typed.declarations)

    template_jq = dict(typed.template_jq or {})
    _check_template_jq(template_jq)
    _check_program_paths(template_jq, defaults_fragment)
    if typed.reconcile is not None:
        _check_reconcile(typed.reconcile)

    return StateTemplate(
        name=name,
        description=typed.description,
        parameters=dict(parameters),
        schema=schema,
        regimes=list(typed.regimes),
        declarations=typed.declarations,
        trace=typed.trace,
        template_jq=template_jq,
        reconcile=typed.reconcile,
    )
