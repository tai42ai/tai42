"""The platform state-template document model, its validation, and the
compose machinery.

A state TEMPLATE is ONE JSON document an operator attaches to a state: a schema fragment
with fillable parameters, per-path writer regimes, attach-time declarations, and a
trace switch. Attaching a template to a state places its fragment at a path once; this
module owns the document SHAPE and the pure transforms the attach and the effective-
schema composer lean on. The document holds only what the platform owns; a consumer
keeps its own documents beside the template under its own kind (validated through its
registered attach validator), and any key outside the platform set is refused here.

Three pure entry points carry the feature:

- :func:`validate_template` parses and checks a raw document against every platform rule,
  raising :class:`~tai42_contract.states.errors.TemplateValidationError` loudly on the
  first violation.
- :func:`substitute_parameters` replaces ``{"$parameter": "<name>"}`` markers in a
  fragment with supplied values.
- :func:`compose_effective_schema` places each attachment's substituted (and, when the
  template traces, ``_trace``-stamped) fragment into a base schema, refusing collisions.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from tai42_contract.states.errors import AttachConflictError, TemplateValidationError
from tai42_contract.states.models import TEMPLATE_NAME_RE
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.jq_util import compile_check

TEMPLATE_KIND = "state-template"
REGIMES = frozenset({"single", "composing", "free"})

# The named jq variables a declarations ``check`` may reference: ``$parameters`` — the
# attachment's effective parameters (defaults overlaid by supplied values), bound at the
# attach seam that evaluates the check. Declared here so an author's check compiles at
# upload and every evaluator binds the same set.
DECLARATIONS_CHECK_VARIABLES: tuple[str, ...] = ("parameters",)

# The five trace fields the effective schema admits on every object under a tracing
# attachment; the platform ``apply`` chokepoint stamps them. ``at`` is always present;
# ``meta``/``run``/``turn``/``inbound`` are null when the writer has none (a hook,
# schedule, api, or a builtin ``state_*`` tool supplies no meta, run, turn, or inbound).
# ``meta`` is the consumer's opaque provenance bag, stored and echoed as an object.
_TRACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "meta": {"type": ["object", "null"]},
        "run": {"type": ["string", "null"]},
        "turn": {"type": ["string", "null"]},
        "inbound": {"type": ["string", "null"]},
        "at": {"type": "string"},
    },
}


# --------------------------------------------------------------------------- #
# Document model (frozen dataclasses — a pydantic model with a ``schema`` field  #
# would shadow ``BaseModel.schema`` and warn, and the suite turns warnings into  #
# errors).                                                                       #
# --------------------------------------------------------------------------- #
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
class TemplateDeclarations:
    """The declarations section: the ``schema`` of the static values an attachment stores,
    and an OPTIONAL ``check`` — a templated text carrying (inline or by stored id) a jq
    predicate over those values returning ``true`` or a message. The check stays platform —
    its rendered jq is evaluated at attach over the declaration values, which are its input,
    with the attachment's EFFECTIVE parameters (template defaults overlaid by supplied
    values) bound as the named jq variable ``$parameters``. A check may therefore constrain
    a declaration against a parameter (e.g. against a parameter-declared enum) at attach, the
    earliest point both are known. Every evaluator of a check MUST supply ``$parameters``; a
    check referencing it without the binding fails loudly (jq: undefined variable
    ``$parameters``)."""

    schema: dict[str, Any]
    check: TemplatedText | None = None


@dataclass(frozen=True, slots=True)
class TemplateJq:
    """A named jq program on a template, of one of two purposes. The program body ``jq`` is a
    :class:`~tai42_contract.template.TemplatedText` — inline ``content`` or a stored ``id`` — so
    an author may hold the program text inline or in a stored resource; it is rendered to its jq
    program immediately before it is compiled or evaluated, never in a validator.

    * ``input``: ``jq`` renders to a program over the record's attached subtree (``.`` = the subject's
      document at the attach path) with the attachment's effective ``$parameters`` and
      ``$declarations`` bound and its declared ``params`` delivered as the SINGLE object
      ``$params`` (``{}`` when none) — a program reads a declared parameter as ``$params.<key>``.
      Read-only; returns any JSON value. An input program may call a sibling input program as
      ``tjq_<name>($params_object)`` (the defs are emitted dependency-first, one per input
      program, each of arity one).
    * ``update``: ``jq`` is a program whose input is ``{record, input}`` (``.record`` = the
      subject's attached subtree, ``.input`` = the adapter's output) with ``$parameters`` and
      ``$declarations`` bound; it returns an ordered op batch (template-relative paths)
      applied through the store. ``reads``/``writes`` are template-relative record paths,
      declared for readability; at put each is validated only to resolve structurally against
      the template fragment, while the write regime itself is enforced at apply by the store
      guard. An update
      program's ``params`` names the keys its ``.input`` object carries — the contract the
      apply seam validates the supplied ``input`` against — and it may call an input program
      as ``tjq_<name>($params_object)``.

    ``params`` defaults empty; ``reads``/``writes`` are empty for an ``input`` program."""

    jq: TemplatedText
    purpose: str
    description: str = ""
    params: list[str] = field(default_factory=list)
    reads: list[list[str]] = field(default_factory=list)
    writes: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TemplateReconcile:
    """How a declarations edit settles a state's OPEN records. Three jq programs, each over
    an input payload (no ``$`` bindings): ``orphans`` over ``{previous, new, data}`` returns
    the ``[{id, label}]`` items a record's subtree orphans against the new declarations;
    ``resolutions`` over ``{new}`` returns the not-done resolution names a close may name;
    ``close`` over ``{data, id, resolution}`` returns the template-relative op batch that
    closes one orphan. ``{orphans, close, resolutions}`` is the reconcile section's own
    vocabulary, distinct from ``template_jq``. Each program body is a
    :class:`~tai42_contract.template.TemplatedText` — inline ``content`` or a stored ``id`` —
    rendered to its jq program immediately before it is compiled or evaluated."""

    orphans: TemplatedText
    close: TemplatedText
    resolutions: TemplatedText


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
    declarations: TemplateDeclarations | None
    trace: TemplateTrace
    template_jq: dict[str, TemplateJq] = field(default_factory=dict)
    reconcile: TemplateReconcile | None = None

    def defaults(self) -> dict[str, Any]:
        """The parameter values applied when an attachment supplies none — only defaulted
        params."""
        return {name: copy.deepcopy(p.default) for name, p in self.parameters.items() if p.has_default}

    def to_document(self) -> dict[str, Any]:
        """The canonical JSON document for this template — the inverse of
        :func:`validate_template`, re-validatable and stable (the seed applier hashes it to
        tell a shipped default apart from an operator edit)."""
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
            declarations: dict[str, Any] = {"schema": self.declarations.schema}
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


def _program_to_document(program: TemplateJq) -> dict[str, Any]:
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


# --------------------------------------------------------------------------- #
# Parameter substitution                                                        #
# --------------------------------------------------------------------------- #
def _is_marker(node: Any) -> bool:
    """Whether ``node`` is a ``{"$parameter": "<name>"}`` fill marker."""
    return isinstance(node, dict) and "$parameter" in node


def _marker_name(node: dict[str, Any]) -> str:
    """The parameter name of a marker, refusing a malformed marker loudly."""
    if len(node) != 1 or not isinstance(node["$parameter"], str) or not node["$parameter"]:
        raise TemplateValidationError(
            f"a $parameter marker must be exactly {{'$parameter': '<name>'}} with a non-empty name, got {node!r}"
        )
    return node["$parameter"]


def substitute_parameters(fragment: Any, values: Mapping[str, Any]) -> Any:
    """Replace every ``{"$parameter": "<name>"}`` marker whose ``<name>`` is in ``values``
    with a deep copy of that value; a marker whose name is absent is left intact (the
    validation path substitutes only DEFAULTS and leaves no-default markers standing).
    Pure — the input is never mutated."""
    if _is_marker(fragment):
        name = _marker_name(fragment)
        return copy.deepcopy(values[name]) if name in values else {"$parameter": name}
    if isinstance(fragment, dict):
        return {k: substitute_parameters(v, values) for k, v in fragment.items()}
    if isinstance(fragment, list):
        return [substitute_parameters(v, values) for v in fragment]
    return fragment


def _iter_marker_names(node: Any):
    """Yield every parameter name referenced by a marker anywhere in ``node``."""
    if _is_marker(node):
        yield _marker_name(node)
        return
    if isinstance(node, dict):
        for v in node.values():
            yield from _iter_marker_names(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_marker_names(v)


# --------------------------------------------------------------------------- #
# Trace injection + effective-schema composition                                #
# --------------------------------------------------------------------------- #
def _inject_trace(schema: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of ``schema`` with a ``_trace`` property added to EVERY object schema
    within it (nested objects, array items, ``additionalProperties`` schemas,
    combinators), so a document validated whole under a tracing attachment admits the
    stamped field even where ``additionalProperties: false`` would otherwise forbid it."""
    node = copy.deepcopy(schema)
    _inject_trace_inplace(node)
    return node


def _inject_trace_inplace(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _inject_trace_inplace(item)
        return
    if not isinstance(node, dict):
        return
    for key in ("properties", "patternProperties", "$defs", "definitions"):
        sub = node.get(key)
        if isinstance(sub, dict):
            for value in sub.values():
                _inject_trace_inplace(value)
    for key in ("items", "additionalProperties", "contains", "propertyNames"):
        sub = node.get(key)
        if isinstance(sub, dict):
            _inject_trace_inplace(sub)
        elif isinstance(sub, list):
            for value in sub:
                _inject_trace_inplace(value)
    for key in ("prefixItems", "allOf", "anyOf", "oneOf"):
        sub = node.get(key)
        if isinstance(sub, list):
            for value in sub:
                _inject_trace_inplace(value)
    if node.get("type") == "object":
        props = node.setdefault("properties", {})
        if isinstance(props, dict):
            props["_trace"] = copy.deepcopy(_TRACE_SCHEMA)


def compose_effective_schema(
    base_schema: dict[str, Any], attachments: Sequence[tuple[StateTemplate, list[str], Mapping[str, Any]]]
) -> dict[str, Any]:
    """The base schema with each attachment's fragment placed at its path.

    Each attachment is ``(template, path, parameters)``: the template's fragment is
    substituted with ``defaults`` overlaid by ``parameters`` (an unsupplied no-default
    marker is a loud refusal), ``_trace``-stamped when the template traces, and placed at
    ``path`` — creating intermediate ``{"type": "object", "properties": {}}`` levels. An
    attachment path that collides with an existing base property, or that overlaps another
    attachment's path, is refused with
    :class:`~tai42_contract.states.errors.AttachConflictError`."""
    for i, (template_a, path_a, _pa) in enumerate(attachments):
        for template_b, path_b, _pb in attachments[i + 1 :]:
            if _paths_prefix_overlap(path_a, path_b):
                raise AttachConflictError(
                    f"attach of template {template_a.name!r} at {path_a} overlaps attach of "
                    f"template {template_b.name!r} at {path_b}"
                )
    result = copy.deepcopy(base_schema)
    for template, path, parameters in attachments:
        values = {**template.defaults(), **dict(parameters or {})}
        fragment = substitute_parameters(template.schema, values)
        leftover = sorted(set(_iter_marker_names(fragment)))
        if leftover:
            raise AttachConflictError(
                f"attach of template {template.name!r} leaves parameter(s) {leftover} unsupplied at {list(path)}"
            )
        if template.trace.enabled:
            fragment = _inject_trace(fragment)
        _place_fragment(result, list(path), fragment, template.name)
    return result


def _paths_prefix_overlap(a: list[str], b: list[str]) -> bool:
    """Whether two concrete attachment paths overlap — one is equal to, or a prefix of, the
    other (attachment paths carry no wildcards)."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _place_fragment(root: dict[str, Any], path: list[str], fragment: dict[str, Any], template_name: str) -> None:
    if not path:
        root_props = root.setdefault("properties", {})
        for key, value in fragment.get("properties", {}).items():
            if key in root_props:
                raise AttachConflictError(
                    f"attach of template {template_name!r} at the root collides with existing property {key!r}"
                )
            root_props[key] = value
        for req in fragment.get("required", []):
            required = root.setdefault("required", [])
            if req not in required:
                required.append(req)
        return
    node = root
    for seg in path[:-1]:
        props = node.setdefault("properties", {})
        child = props.get(seg)
        if child is None:
            child = {"type": "object", "properties": {}}
            props[seg] = child
        elif not (isinstance(child, dict) and child.get("type") == "object"):
            raise AttachConflictError(
                f"attach of template {template_name!r} at {path} passes through non-object property {seg!r}"
            )
        node = child
    props = node.setdefault("properties", {})
    last = path[-1]
    if last in props:
        raise AttachConflictError(
            f"attach of template {template_name!r} at {path} collides with existing property {last!r}"
        )
    props[last] = fragment


# --------------------------------------------------------------------------- #
# Regimes                                                                        #
# --------------------------------------------------------------------------- #
def _pattern_prefix_matches(pattern: list[str], path: list[Any]) -> bool:
    """Whether ``pattern`` (with ``"*"`` wildcards) matches a leading run of ``path`` —
    the regime is declared AT or ABOVE the fill; ``"*"`` matches one index or key."""
    if len(pattern) > len(path):
        return False
    return all(seg == "*" or seg == path[i] for i, seg in enumerate(pattern))


def regime_for(template: StateTemplate, relative_path: list[Any]) -> str:
    """The regime governing ``relative_path`` — the ``regime`` of the LONGEST (most
    specific) declared regime path that matches it as a prefix, else ``"free"``."""
    best = "free"
    best_len = -1
    for rule in template.regimes:
        if _pattern_prefix_matches(rule.path, relative_path) and len(rule.path) > best_len:
            best = rule.regime
            best_len = len(rule.path)
    return best


def path_overlaps(a: list[Any], b: list[Any]) -> bool:
    """Whether two paths overlap — equal, one a prefix/descendant of the other —
    comparing ``"*"`` in either as a match for one segment on the other side."""
    n = min(len(a), len(b))
    return all(a[i] == "*" or b[i] == "*" or a[i] == b[i] for i in range(n))


def _validate_regime_path(fragment: dict[str, Any], path: list[str]) -> None:
    """Walk a regime ``path`` statically over the (defaults-substituted) fragment: a
    literal key must be a declared property (or admitted by an open object), and ``"*"``
    is allowed ONLY where the schema has ``items`` or ``additionalProperties``. A
    no-default parameter marker is opaque — traversal into it accepts the remaining
    segments."""
    node: Any = fragment
    for seg in path:
        if _is_marker(node):
            return
        if not isinstance(node, dict):
            raise TemplateValidationError(f"regime path {path} descends past the fragment's structure at {seg!r}")
        if seg == "*":
            items = node.get("items")
            addl = node.get("additionalProperties")
            if isinstance(items, dict):
                node = items
            elif isinstance(addl, dict):
                node = addl
            elif addl is True or isinstance(node.get("patternProperties"), dict):
                node = {}
            else:
                raise TemplateValidationError(
                    f"regime path {path} uses '*' where the fragment has no items or additionalProperties"
                )
        else:
            props = node.get("properties")
            addl = node.get("additionalProperties")
            if isinstance(props, dict) and seg in props:
                node = props[seg]
            elif isinstance(addl, dict):
                node = addl
            elif addl is True:
                node = {}
            else:
                raise TemplateValidationError(
                    f"regime path segment {seg!r} in {path} is not a property of the fragment"
                )


# --------------------------------------------------------------------------- #
# The document validator                                                        #
# --------------------------------------------------------------------------- #
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
    """Refuse any key outside the platform document. A consumer keeps its own documents
    beside the template under its own kind, validated through its registered attach
    validator; nothing consumer-owned is folded into the state-template document."""
    extra = sorted(set(doc) - _TEMPLATE_KEYS)
    if extra:
        raise TemplateValidationError(
            f"{where} carries unknown key(s) {extra}; a state-template document holds "
            "schema, parameters, regimes, declarations, trace, template_jq, reconcile"
        )


def _compile_check_jq(expr: str, *, where: str) -> None:
    """Compile-check the declarations ``check`` predicate, declaring the named variables
    the attach seam binds at evaluation (``$parameters`` — the effective attach parameters)
    so an author may reference them; a failure is a loud template error."""
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


def _parse_declarations(raw: Any) -> TemplateDeclarations:
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
    return TemplateDeclarations(schema=schema, check=check)


# The named jq variables a ``template_jq`` program body may reference beyond its own
# params: the attachment's effective ``$parameters`` and its ``$declarations``, bound at
# the evaluator seam that runs the program. Declared here so a program compiles at upload
# and every evaluator binds the same set.
MEMBER_JQ_VARIABLES: tuple[str, ...] = ("parameters", "declarations")


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
    """A list of template-relative record paths (an update program's ``reads``/``writes``);
    each path a list of non-empty string segments (object keys or the ``"*"`` wildcard)."""
    value = _require_type(raw, list, where=where)
    out: list[list[str]] = []
    for i, path in enumerate(value):
        seg_list = _require_type(path, list, where=f"{where}[{i}]")
        for seg in seg_list:
            if not isinstance(seg, str) or not seg:
                raise TemplateValidationError(f"{where}[{i}] segment {seg!r} must be a non-empty string ('*' or a key)")
        out.append(list(seg_list))
    return out


def _input_def(name: str, jq: str) -> str:
    """One sibling def ``def tjq_<name>($params): <jq>;`` an input program may call as
    ``tjq_<name>({…})`` — always arity one, the declared params delivered as the single
    ``$params`` object (``{}`` when the program declares none). ``jq`` is the input program's
    RENDERED body text."""
    return f"def tjq_{name}($params): {jq}; "


def _references(expr: str, name: str) -> bool:
    """Whether ``expr`` names ``name`` as a jq token (a call or a bare reference)."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", expr) is not None


def _input_order(rendered_inputs: Mapping[str, str]) -> list[str]:
    """A deterministic dependency-first ordering of input-program names, keyed on each
    program's RENDERED body text: a program that references a sibling is emitted AFTER it (jq
    resolves names backward only). A reference cycle — which jq cannot express — falls back to
    sorted order, so the compile fails loudly at validation rather than silently."""
    names = sorted(rendered_inputs)
    # A sibling is called by its prelude def name ``tjq_<name>``; a def that calls another
    # must be emitted AFTER it (jq resolves names backward only).
    deps = {a: {b for b in names if b != a and _references(rendered_inputs[a], f"tjq_{b}")} for a in names}
    ordered: list[str] = []
    remaining = set(names)
    while remaining:
        ready = sorted(n for n in remaining if deps[n] <= set(ordered))
        if not ready:
            ordered.extend(sorted(remaining))
            break
        ordered.append(ready[0])
        remaining.discard(ready[0])
    return ordered


def template_jq_prelude(rendered_inputs: Mapping[str, str]) -> str:
    """The dependency-first run of ``def tjq_<name>($params): <jq>;`` sibling declarations, built
    from the RENDERED body text of the template's INPUT-purpose programs (``name -> rendered
    jq``) — prepended to a program's jq at compile and at evaluation so a reference to a sibling
    input program resolves. Empty when the template declares no input programs. The bodies are
    rendered at the point of use (the save door and the evaluator seam); this function is pure
    over already-rendered text."""
    return "".join(_input_def(name, rendered_inputs[name]) for name in _input_order(rendered_inputs))


def _parse_program_body(raw: Any, *, where: str) -> TemplatedText:
    """Parse one authored jq program body as a :class:`~tai42_contract.template.TemplatedText`
    (inline ``content`` or a stored ``id``). A stray key inside the value is refused by the
    value type (``extra="forbid"``), and an empty inline body is a loud refusal — a bad shape or
    an empty program is the same loud :class:`TemplateValidationError` a malformed section is."""
    _require_type(raw, dict, where=where)
    try:
        text = TemplatedText.model_validate(raw)
    except ValidationError as exc:
        raise TemplateValidationError(f"{where} is not a valid templated text: {exc}") from exc
    if text.content is not None and not text.content.strip():
        raise TemplateValidationError(f"{where} must be a non-empty jq program")
    return text


def _compile_template_jq_inline(programs: Mapping[str, TemplateJq]) -> None:
    """Compile every ``template_jq`` program at upload when EVERY body is inline — the point the
    full sibling prelude is known without a fetch. A by-id body anywhere in the section defers
    the whole section's compile to the save door (:meth:`StatesService._compile_by_id_template_jq`),
    the point that can render the stored resources; nothing is silently skipped. Each program
    compiles over the sibling INPUT-purpose prelude, with ``$parameters``/``$declarations`` bound
    and — for an input program — the single ``$params`` object predeclared; a reference cycle
    among input programs (which jq cannot express) fails the compile loudly."""
    if not all(p.jq.content is not None for p in programs.values()):
        return
    rendered_inputs = {name: p.jq.content for name, p in programs.items() if p.purpose == "input" and p.jq.content}
    prelude = template_jq_prelude(rendered_inputs)
    for name, program in programs.items():
        assert program.jq.content is not None  # every body inline in this branch
        # An input program's declared params ride the SINGLE ``$params`` object, never
        # individual ``$<name>`` args; an update program reads ``{record, input}`` as ``.`` and
        # declares its ``.input`` keys as ``params`` (validated at apply, not bound here).
        variables = (*MEMBER_JQ_VARIABLES, "params") if program.purpose == "input" else MEMBER_JQ_VARIABLES
        try:
            compile_check(prelude + program.jq.content, variables=variables)
        except Exception as exc:
            raise TemplateValidationError(f"template_jq {name!r} jq is not a valid jq expression: {exc}") from exc


def _parse_template_jq(raw: Any) -> dict[str, TemplateJq]:
    """Parse the ``template_jq`` section: each entry ``{description?, purpose, ...}`` with a
    ``purpose`` of ``input`` or ``update``. Both purposes may declare ``params``; an
    ``input`` entry carries no ``reads``/``writes``, an ``update`` entry carries them. Each
    entry's ``jq`` is a :class:`~tai42_contract.template.TemplatedText` (inline ``content`` or a
    stored ``id``). An all-inline section compiles here over the sibling INPUT-purpose prelude
    (so any program may call an input program as ``tjq_<name>({…})``); a by-id body defers the
    section's compile to the save door, which can render the stored resources. ``reads``/``writes``
    are template-relative paths (checked against the fragment in :func:`validate_template`)."""
    _require_type(raw, dict, where="template_jq")
    programs: dict[str, TemplateJq] = {}
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
            programs[name] = TemplateJq(jq=jq, purpose="input", description=description, params=params)
        else:
            _reject_section_extra_keys(
                spec, frozenset({"description", "purpose", "params", "reads", "writes", "jq"}), where=where
            )
            reads = _parse_path_list(spec.get("reads", []), where=f"{where} reads")
            writes = _parse_path_list(spec.get("writes", []), where=f"{where} writes")
            programs[name] = TemplateJq(
                jq=jq, purpose="update", description=description, params=params, reads=reads, writes=writes
            )
    _compile_template_jq_inline(programs)
    return programs


def _parse_reconcile(raw: Any) -> TemplateReconcile:
    """Parse the ``reconcile`` section ``{orphans, close, resolutions}`` — three jq programs,
    each a :class:`~tai42_contract.template.TemplatedText` (inline ``content`` or a stored ``id``)
    over its own input payload (no ``$`` bindings). An inline body compiles here; a by-id body
    defers its compile to the save door (:meth:`StatesService._compile_by_id_reconcile`), the
    point that can render the stored resource."""
    _require_type(raw, dict, where="reconcile")
    _reject_section_extra_keys(raw, frozenset({"orphans", "close", "resolutions"}), where="reconcile")
    programs: dict[str, TemplatedText] = {}
    for label in ("orphans", "close", "resolutions"):
        text = _parse_program_body(raw.get(label), where=f"reconcile {label}")
        if text.content is not None:
            try:
                compile_check(text.content)
            except Exception as exc:
                raise TemplateValidationError(f"reconcile {label} is not a valid jq expression: {exc}") from exc
        programs[label] = text
    return TemplateReconcile(orphans=programs["orphans"], close=programs["close"], resolutions=programs["resolutions"])


def _parse_trace(raw: Any) -> TemplateTrace:
    _require_type(raw, dict, where="trace")
    _reject_section_extra_keys(raw, frozenset({"enabled"}), where="trace")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TemplateValidationError(f"trace enabled must be a boolean, got {enabled!r}")
    return TemplateTrace(enabled=enabled)


def _validate_fragment_schema(name: str, fragment: dict[str, Any]) -> None:
    """The fragment, with defaults substituted, must pass the states service's object-
    schema validator (object-rooted, ≥1 property, a valid draft 2020-12 schema)."""
    # Imported lazily: the service imports this module, so a top-level import here would
    # close a cycle. ``_validate_schema`` is pure.
    from tai42_contract.states.errors import SchemaValidationError

    from tai42_skeleton.states.service import _validate_schema

    try:
        _validate_schema(fragment)
    except SchemaValidationError as exc:
        raise TemplateValidationError(f"template {name!r} schema fragment is not a valid object schema: {exc}") from exc


def validate_template(doc: Any) -> StateTemplate:
    """Parse and validate a raw platform state-template document, returning the
    :class:`StateTemplate`.

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
    :class:`~tai42_contract.states.errors.TemplateValidationError` on the first violation."""
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


__all__ = [
    "MEMBER_JQ_VARIABLES",
    "REGIMES",
    "TEMPLATE_KIND",
    "RegimeRule",
    "StateTemplate",
    "TemplateDeclarations",
    "TemplateJq",
    "TemplateParameter",
    "TemplateReconcile",
    "TemplateTrace",
    "compose_effective_schema",
    "path_overlaps",
    "regime_for",
    "substitute_parameters",
    "template_jq_prelude",
    "validate_template",
]
