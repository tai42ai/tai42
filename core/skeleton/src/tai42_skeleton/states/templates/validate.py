"""The platform state-template document parser and validator.

Parses a raw JSON document into a :class:`~tai42_skeleton.states.templates.model.StateTemplate`,
enforcing every platform rule and refusing any key outside the platform set. A consumer keeps
its own documents beside the template under its own kind (validated through its registered
attach validator); nothing consumer-owned is folded into this document.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError
from tai42_contract.states.errors import SchemaValidationError, TemplateValidationError
from tai42_contract.states.models import (
    TEMPLATE_NAME_RE,
    StateTemplateDeclarations,
    StateTemplateJq,
    StateTemplateReconcile,
)
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.jq_util import compile_check

from tai42_skeleton.states.schema import _validate_schema
from tai42_skeleton.states.templates.jq import _compile_template_jq_inline
from tai42_skeleton.states.templates.model import (
    TEMPLATE_KIND,
    RegimeRule,
    StateTemplate,
    TemplateParameter,
    TemplateTrace,
)
from tai42_skeleton.states.templates.parameters import _iter_marker_names, substitute_parameters
from tai42_skeleton.states.templates.regimes import REGIMES, _validate_regime_path

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

_TEMPLATE_KEYS = frozenset(
    {
        "kind",
        "name",
        "description",
        "parameters",
        "schema",
        "regimes",
        "declarations",
        "trace",
        "template_jq",
        "reconcile",
    }
)

#: A ``template_jq`` program name: a lowercase identifier — the readable handle an
#: ``input`` program is called by (``<name>(…)``), and a jq-safe function name.
_MEMBER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _require_type(value: Any, kind: type | tuple[type, ...], *, where: str) -> Any:
    if isinstance(value, bool) and kind is not bool and bool not in (kind if isinstance(kind, tuple) else (kind,)):
        raise TemplateValidationError(f"{where} must not be a boolean")
    if not isinstance(value, kind):
        raise TemplateValidationError(
            f"{where} must be a {getattr(kind, '__name__', kind)}, got {type(value).__name__}"
        )
    return value


def _reject_extra_keys(doc: dict[str, Any], *, where: str) -> None:
    """Refuse any key outside the platform document.

    A consumer keeps its own documents beside the template under its own kind, validated through its
    registered attach validator; nothing consumer-owned is folded into the state-template document.
    """
    extra = sorted(set(doc) - _TEMPLATE_KEYS)
    if extra:
        raise TemplateValidationError(
            f"{where} carries unknown key(s) {extra}; a state-template document holds "
            "schema, parameters, regimes, declarations, trace, template_jq, reconcile"
        )


def _compile_check_jq(expr: str, *, where: str) -> None:
    """Compile-check the declarations ``check`` predicate.

    Declares the named variables the attach seam binds at evaluation (``$parameters`` — the
    effective attach parameters) so an author may reference them; a failure is a loud template error.
    """
    try:
        compile_check(expr, variables=DECLARATIONS_CHECK_VARIABLES)
    except Exception as exc:
        raise TemplateValidationError(f"{where} is not a valid jq expression: {exc}") from exc


def _reject_section_extra_keys(doc: dict[str, Any], allowed: frozenset[str], *, where: str) -> None:
    extra = sorted(set(doc) - allowed)
    if extra:
        raise TemplateValidationError(f"{where} carries unknown key(s) {extra}")


def _parse_parameters(raw: Any) -> dict[str, TemplateParameter]:
    _require_type(raw, dict, where="parameters")
    out: dict[str, TemplateParameter] = {}
    for name, spec in raw.items():
        where = f"parameter {name!r}"
        _require_type(spec, dict, where=where)
        _reject_section_extra_keys(spec, frozenset({"schema", "default"}), where=where)
        schema = _require_type(spec.get("schema"), dict, where=f"{where} schema")
        if "default" in spec:
            out[name] = TemplateParameter(schema=schema, has_default=True, default=spec["default"])
        else:
            out[name] = TemplateParameter(schema=schema, has_default=False)
    return out


def _parse_regimes(raw: Any) -> list[RegimeRule]:
    _require_type(raw, list, where="regimes")
    rules: list[RegimeRule] = []
    for i, entry in enumerate(raw):
        where = f"regimes[{i}]"
        _require_type(entry, dict, where=where)
        _reject_section_extra_keys(entry, frozenset({"path", "regime"}), where=where)
        path = _require_type(entry.get("path"), list, where=f"{where} path")
        for seg in path:
            if not isinstance(seg, str) or not seg:
                raise TemplateValidationError(f"{where} path segment {seg!r} must be a non-empty string ('*' or a key)")
        regime = entry.get("regime")
        if regime not in REGIMES:
            raise TemplateValidationError(f"{where} regime {regime!r} must be one of {sorted(REGIMES)}")
        rules.append(RegimeRule(path=list(path), regime=regime))
    return rules


def _parse_declarations(raw: Any) -> StateTemplateDeclarations:
    _require_type(raw, dict, where="declarations")
    _reject_section_extra_keys(raw, frozenset({"schema", "check"}), where="declarations")
    schema = _require_type(raw.get("schema"), dict, where="declarations schema")
    raw_check = raw.get("check")
    check: TemplatedText | None = None
    if raw_check is not None:
        _require_type(raw_check, dict, where="declarations check")
        try:
            check = TemplatedText.model_validate(raw_check)
        except ValidationError as exc:
            raise TemplateValidationError(f"declarations check is not a valid templated text: {exc}") from exc
        # Inline jq compiles here (pure, no fetch); a by-id check is rendered and compiled at
        # the save door (``put_template``), which can fetch the stored resource.
        if check.content is not None:
            if not check.content.strip():
                raise TemplateValidationError("declarations check must be a non-empty jq predicate or omitted")
            _compile_check_jq(check.content, where="declarations check")
    return StateTemplateDeclarations(schema=schema, check=check)


def _parse_identifier_list(raw: Any, *, where: str) -> list[str]:
    """A list of unique identifier strings (an input program's ``params``)."""
    value = _require_type(raw, list, where=where)
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _MEMBER_NAME_RE.fullmatch(item):
            raise TemplateValidationError(
                f"{where} entry {item!r} must be an identifier matching {_MEMBER_NAME_RE.pattern}"
            )
        if item in seen:
            raise TemplateValidationError(f"{where} names {item!r} more than once")
        seen.add(item)
        out.append(item)
    return out


def _parse_path_list(raw: Any, *, where: str) -> list[list[str]]:
    """A list of template-relative record paths (an update program's ``reads``/``writes``).

    Each path a list of non-empty string segments (object keys or the ``"*"`` wildcard).
    """
    value = _require_type(raw, list, where=where)
    out: list[list[str]] = []
    for i, path in enumerate(value):
        seg_list = _require_type(path, list, where=f"{where}[{i}]")
        for seg in seg_list:
            if not isinstance(seg, str) or not seg:
                raise TemplateValidationError(f"{where}[{i}] segment {seg!r} must be a non-empty string ('*' or a key)")
        out.append(list(seg_list))
    return out


def _parse_program_body(raw: Any, *, where: str) -> TemplatedText:
    """Parse one authored jq program body as a :class:`~tai42_contract.template.TemplatedText`.

    Inline ``content`` or a stored ``id``. A stray key inside the value is refused by the
    value type (``extra="forbid"``), and an empty inline body is a loud refusal — a bad shape or
    an empty program is the same loud :class:`TemplateValidationError` a malformed section is.
    """
    _require_type(raw, dict, where=where)
    try:
        text = TemplatedText.model_validate(raw)
    except ValidationError as exc:
        raise TemplateValidationError(f"{where} is not a valid templated text: {exc}") from exc
    if text.content is not None and not text.content.strip():
        raise TemplateValidationError(f"{where} must be a non-empty jq program")
    return text


def _parse_template_jq(raw: Any) -> dict[str, StateTemplateJq]:
    """Parse the ``template_jq`` section.

    Each entry ``{description?, purpose, ...}`` with a ``purpose`` of ``input`` or ``update``. Both
    purposes may declare ``params``; an ``input`` entry carries no ``reads``/``writes``, an
    ``update`` entry carries them. Each entry's ``jq`` is a
    :class:`~tai42_contract.template.TemplatedText` (inline ``content`` or a stored ``id``). An
    all-inline section compiles here over the sibling INPUT-purpose prelude (so any program may call
    an input program as ``tjq_<name>({…})``); a by-id body defers the section's compile to the save
    door, which can render the stored resources. ``reads``/``writes`` are template-relative paths
    (checked against the fragment in :func:`validate_template`).
    """
    _require_type(raw, dict, where="template_jq")
    programs: dict[str, StateTemplateJq] = {}
    for name, spec in raw.items():
        where = f"template_jq {name!r}"
        if not _MEMBER_NAME_RE.fullmatch(name):
            raise TemplateValidationError(f"template_jq name {name!r} must match {_MEMBER_NAME_RE.pattern}")
        _require_type(spec, dict, where=where)
        purpose = spec.get("purpose")
        if purpose not in ("input", "update"):
            raise TemplateValidationError(f"{where} purpose {purpose!r} must be 'input' or 'update'")
        jq = _parse_program_body(spec.get("jq"), where=f"{where} jq")
        description = _require_type(spec.get("description", ""), str, where=f"{where} description")
        params = _parse_identifier_list(spec.get("params", []), where=f"{where} params")
        if purpose == "input":
            _reject_section_extra_keys(spec, frozenset({"description", "purpose", "params", "jq"}), where=where)
            programs[name] = StateTemplateJq(jq=jq, purpose="input", description=description, params=params)
        else:
            _reject_section_extra_keys(
                spec, frozenset({"description", "purpose", "params", "reads", "writes", "jq"}), where=where
            )
            reads = _parse_path_list(spec.get("reads", []), where=f"{where} reads")
            writes = _parse_path_list(spec.get("writes", []), where=f"{where} writes")
            programs[name] = StateTemplateJq(
                jq=jq, purpose="update", description=description, params=params, reads=reads, writes=writes
            )
    _compile_template_jq_inline(programs)
    return programs


def _parse_reconcile(raw: Any) -> StateTemplateReconcile:
    """Parse the ``reconcile`` section ``{orphans, close, resolutions}`` — three jq programs.

    Each is a :class:`~tai42_contract.template.TemplatedText` (inline ``content`` or a stored ``id``)
    over its own ``.`` with the per-label :data:`RECONCILE_JQ_VARIABLES` bound as ``$name``
    beside it. An inline body compiles here; a by-id body defers its compile to the save door
    (:meth:`~tai42_skeleton.states.service.StatesService._compile_by_id_reconcile`), the point
    that can render the stored resource.
    """
    _require_type(raw, dict, where="reconcile")
    _reject_section_extra_keys(raw, frozenset({"orphans", "close", "resolutions"}), where="reconcile")
    programs: dict[str, TemplatedText] = {}
    for label in ("orphans", "close", "resolutions"):
        text = _parse_program_body(raw.get(label), where=f"reconcile {label}")
        if text.content is not None:
            try:
                compile_check(text.content, variables=RECONCILE_JQ_VARIABLES[label])
            except Exception as exc:
                raise TemplateValidationError(f"reconcile {label} is not a valid jq expression: {exc}") from exc
        programs[label] = text
    return StateTemplateReconcile(
        orphans=programs["orphans"], close=programs["close"], resolutions=programs["resolutions"]
    )


def _parse_trace(raw: Any) -> TemplateTrace:
    _require_type(raw, dict, where="trace")
    _reject_section_extra_keys(raw, frozenset({"enabled"}), where="trace")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TemplateValidationError(f"trace enabled must be a boolean, got {enabled!r}")
    return TemplateTrace(enabled=enabled)


def _validate_fragment_schema(name: str, fragment: dict[str, Any]) -> None:
    """The fragment, with defaults substituted, must pass the shared object-schema validator.

    Object-rooted, ≥1 property, a valid draft 2020-12 schema.
    """
    try:
        _validate_schema(fragment)
    except SchemaValidationError as exc:
        raise TemplateValidationError(f"template {name!r} schema fragment is not a valid object schema: {exc}") from exc


def validate_template(doc: Any) -> StateTemplate:
    """Parse and validate a raw platform state-template document into a :class:`StateTemplate`.

    Enforces every platform rule: ``kind == "state-template"``; the ``name`` form; the
    fragment is an object schema passing ``_validate_schema`` after defaults substitution
    (a no-default parameter must appear as a marker, and every marker names a declared
    parameter); regime paths lie inside the fragment (``"*"`` only over
    ``items``/``additionalProperties``); the declarations schema is an object and its
    optional ``check`` is a templated text whose inline jq compiles (a by-id check is
    rendered and compiled at the save door, which can fetch its stored resource); each
    ``template_jq`` program's ``jq`` and each ``reconcile`` program are templated texts —
    an all-inline ``template_jq`` section compiles over the sibling input-program prelude (so
    any program may call an input program; a reference cycle among input programs fails loudly)
    and an inline ``reconcile`` program compiles, while a by-id body is rendered and compiled at
    the save door; every update program's ``reads``/``writes`` paths resolve in the fragment. Any
    key outside the platform set is refused. Raises
    :class:`~tai42_contract.states.errors.TemplateValidationError` on the first violation.
    """
    _require_type(doc, dict, where="template document")
    _reject_extra_keys(doc, where="template document")

    if doc.get("kind") != TEMPLATE_KIND:
        raise TemplateValidationError(f"template kind must be {TEMPLATE_KIND!r}, got {doc.get('kind')!r}")

    name = _require_type(doc.get("name"), str, where="name")
    if not TEMPLATE_NAME_RE.fullmatch(name):
        raise TemplateValidationError(f"template name {name!r} must match {TEMPLATE_NAME_RE.pattern}")

    description = _require_type(doc.get("description", ""), str, where="description")
    parameters = _parse_parameters(doc.get("parameters", {}))
    schema = _require_type(doc.get("schema"), dict, where="schema")

    marker_names = set(_iter_marker_names(schema))
    unknown = sorted(marker_names - set(parameters))
    if unknown:
        raise TemplateValidationError(f"schema references undeclared parameter(s) {unknown}")
    for pname, param in parameters.items():
        if not param.has_default and pname not in marker_names:
            raise TemplateValidationError(
                f"parameter {pname!r} has no default, so it must appear as a $parameter marker in the fragment"
            )

    defaults_fragment = substitute_parameters(schema, {n: p.default for n, p in parameters.items() if p.has_default})
    _validate_fragment_schema(name, defaults_fragment)

    regimes = _parse_regimes(doc.get("regimes", []))
    for rule in regimes:
        _validate_regime_path(defaults_fragment, rule.path)

    declarations = _parse_declarations(doc["declarations"]) if "declarations" in doc else None
    trace = _parse_trace(doc.get("trace", {}))

    template_jq = _parse_template_jq(doc["template_jq"]) if "template_jq" in doc else {}
    for program_name, program in template_jq.items():
        for path in (*program.reads, *program.writes):
            try:
                _validate_regime_path(defaults_fragment, path)
            except TemplateValidationError as exc:
                raise TemplateValidationError(f"template_jq {program_name!r}: {exc}") from exc
    reconcile = _parse_reconcile(doc["reconcile"]) if "reconcile" in doc else None

    return StateTemplate(
        name=name,
        description=description,
        parameters=parameters,
        schema=schema,
        regimes=regimes,
        declarations=declarations,
        trace=trace,
        template_jq=template_jq,
        reconcile=reconcile,
    )
