"""The platform state-module document model, its validation, and the
compose machinery.

A state MODULE is ONE JSON document an operator mounts on a state: a schema fragment
with fillable parameters, per-path writer regimes, mount-time declarations, and a
trace switch. Mounting a module on a state places its fragment at a path once; this
module owns the document SHAPE and the pure transforms the mount and the effective-
schema composer lean on. The document holds only what the platform owns; a consumer
keeps its own documents beside the module under its own kind (validated through its
registered mount validator), and any key outside the platform set is refused here.

Three pure entry points carry the feature:

- :func:`validate_module` parses and checks a raw document against every platform rule,
  raising :class:`~tai42_contract.states.errors.ModuleValidationError` loudly on the
  first violation.
- :func:`substitute_parameters` replaces ``{"$parameter": "<name>"}`` markers in a
  fragment with supplied values.
- :func:`compose_effective_schema` places each mount's substituted (and, when the
  module traces, ``_trace``-stamped) fragment into a base schema, refusing collisions.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tai42_contract.states.errors import ModuleValidationError, MountConflictError
from tai42_contract.states.models import MODULE_NAME_RE
from tai42_kit.utils.data.jq_util import get_compiled_jq

MODULE_KIND = "state-module"
REGIMES = frozenset({"single", "composing", "free"})

# The five trace fields the effective schema admits on every object under a tracing
# mount; the platform ``apply`` chokepoint stamps them. ``at`` is always present;
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
class ModuleParameter:
    """A fillable parameter: its value ``schema`` and an OPTIONAL ``default``. A
    parameter without a default must be referenced by a marker in the fragment and
    supplied at mount; ``has_default`` distinguishes an absent default from an explicit
    ``null`` default."""

    schema: dict[str, Any]
    has_default: bool = False
    default: Any = None


@dataclass(frozen=True, slots=True)
class RegimeRule:
    """One per-path writer rule: a module-relative ``path`` (object keys and the ``"*"``
    wildcard, which matches one list index or key) and its ``regime``
    (``single`` / ``composing`` / ``free``). An undeclared path is ``free``."""

    path: list[str]
    regime: str


@dataclass(frozen=True, slots=True)
class ModuleDeclarations:
    """The declarations section: the ``schema`` of the static values a mount stores, and
    an OPTIONAL ``check`` (a jq predicate over those values returning ``true`` or a
    message). The check stays platform — it is evaluated at mount over the declaration
    values."""

    schema: dict[str, Any]
    check: str | None = None


@dataclass(frozen=True, slots=True)
class ModuleTrace:
    """The trace switch: when ``enabled``, the effective schema admits ``_trace`` and the
    platform ``apply`` chokepoint stamps it on every write under a mount of this module."""

    enabled: bool = False


@dataclass(frozen=True, slots=True)
class StateModule:
    """A validated platform state-module document. ``schema`` is the object-schema
    fragment (with ``$parameter`` markers); ``defaults`` are the parameter values applied
    when a mount supplies none."""

    name: str
    description: str
    parameters: dict[str, ModuleParameter]
    schema: dict[str, Any]
    regimes: list[RegimeRule]
    declarations: ModuleDeclarations | None
    trace: ModuleTrace

    def defaults(self) -> dict[str, Any]:
        """The parameter values applied when a mount supplies none — only defaulted params."""
        return {name: copy.deepcopy(p.default) for name, p in self.parameters.items() if p.has_default}

    def to_document(self) -> dict[str, Any]:
        """The canonical JSON document for this module — the inverse of
        :func:`validate_module`, re-validatable and stable (the seed applier hashes it to
        tell a shipped default apart from an operator edit)."""
        doc: dict[str, Any] = {"kind": MODULE_KIND, "name": self.name, "description": self.description}
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
                declarations["check"] = self.declarations.check
            doc["declarations"] = declarations
        if self.trace.enabled:
            doc["trace"] = {"enabled": True}
        return doc


# --------------------------------------------------------------------------- #
# Parameter substitution                                                        #
# --------------------------------------------------------------------------- #
def _is_marker(node: Any) -> bool:
    """Whether ``node`` is a ``{"$parameter": "<name>"}`` fill marker."""
    return isinstance(node, dict) and "$parameter" in node


def _marker_name(node: dict[str, Any]) -> str:
    """The parameter name of a marker, refusing a malformed marker loudly."""
    if len(node) != 1 or not isinstance(node["$parameter"], str) or not node["$parameter"]:
        raise ModuleValidationError(
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
    combinators), so a document validated whole under a tracing mount admits the stamped
    field even where ``additionalProperties: false`` would otherwise forbid it."""
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
    base_schema: dict[str, Any], mounts: Sequence[tuple[StateModule, list[str], Mapping[str, Any]]]
) -> dict[str, Any]:
    """The base schema with each mount's fragment placed at its path.

    Each mount is ``(module, path, parameters)``: the module's fragment is substituted
    with ``defaults`` overlaid by ``parameters`` (an unsupplied no-default marker is a
    loud refusal), ``_trace``-stamped when the module traces, and placed at ``path`` —
    creating intermediate ``{"type": "object", "properties": {}}`` levels. A mount path
    that collides with an existing base property, or that overlaps another mount's path,
    is refused with :class:`~tai42_contract.states.errors.MountConflictError`."""
    for i, (module_a, path_a, _pa) in enumerate(mounts):
        for module_b, path_b, _pb in mounts[i + 1 :]:
            if _paths_prefix_overlap(path_a, path_b):
                raise MountConflictError(
                    f"mount of module {module_a.name!r} at {path_a} overlaps mount of "
                    f"module {module_b.name!r} at {path_b}"
                )
    result = copy.deepcopy(base_schema)
    for module, path, parameters in mounts:
        values = {**module.defaults(), **dict(parameters or {})}
        fragment = substitute_parameters(module.schema, values)
        leftover = sorted(set(_iter_marker_names(fragment)))
        if leftover:
            raise MountConflictError(
                f"mount of module {module.name!r} leaves parameter(s) {leftover} unsupplied at {list(path)}"
            )
        if module.trace.enabled:
            fragment = _inject_trace(fragment)
        _place_fragment(result, list(path), fragment, module.name)
    return result


def _paths_prefix_overlap(a: list[str], b: list[str]) -> bool:
    """Whether two concrete mount paths overlap — one is equal to, or a prefix of, the
    other (mount paths carry no wildcards)."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _place_fragment(root: dict[str, Any], path: list[str], fragment: dict[str, Any], module_name: str) -> None:
    if not path:
        root_props = root.setdefault("properties", {})
        for key, value in fragment.get("properties", {}).items():
            if key in root_props:
                raise MountConflictError(
                    f"mount of module {module_name!r} at the root collides with existing property {key!r}"
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
            raise MountConflictError(
                f"mount of module {module_name!r} at {path} passes through non-object property {seg!r}"
            )
        node = child
    props = node.setdefault("properties", {})
    last = path[-1]
    if last in props:
        raise MountConflictError(f"mount of module {module_name!r} at {path} collides with existing property {last!r}")
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


def regime_for(module: StateModule, relative_path: list[Any]) -> str:
    """The regime governing ``relative_path`` — the ``regime`` of the LONGEST (most
    specific) declared regime path that matches it as a prefix, else ``"free"``."""
    best = "free"
    best_len = -1
    for rule in module.regimes:
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
            raise ModuleValidationError(f"regime path {path} descends past the fragment's structure at {seg!r}")
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
                raise ModuleValidationError(
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
                raise ModuleValidationError(f"regime path segment {seg!r} in {path} is not a property of the fragment")


# --------------------------------------------------------------------------- #
# The document validator                                                        #
# --------------------------------------------------------------------------- #
_MODULE_KEYS = frozenset({"kind", "name", "description", "parameters", "schema", "regimes", "declarations", "trace"})


def _require_type(value: Any, kind: type | tuple[type, ...], *, where: str) -> Any:
    if isinstance(value, bool) and kind is not bool and bool not in (kind if isinstance(kind, tuple) else (kind,)):
        raise ModuleValidationError(f"{where} must not be a boolean")
    if not isinstance(value, kind):
        raise ModuleValidationError(f"{where} must be a {getattr(kind, '__name__', kind)}, got {type(value).__name__}")
    return value


def _reject_extra_keys(doc: dict[str, Any], *, where: str) -> None:
    """Refuse any key outside the platform document. A consumer keeps its own documents
    beside the module under its own kind, validated through its registered mount
    validator; nothing consumer-owned is folded into the state-module document."""
    extra = sorted(set(doc) - _MODULE_KEYS)
    if extra:
        raise ModuleValidationError(
            f"{where} carries unknown key(s) {extra}; a state-module document holds "
            "schema, parameters, regimes, declarations, trace"
        )


def _compile_jq(expr: str, *, where: str) -> None:
    """Compile-check one jq expression on its own; a failure is a loud module error."""
    try:
        get_compiled_jq(expr)
    except Exception as exc:
        raise ModuleValidationError(f"{where} is not a valid jq expression: {exc}") from exc


def _reject_section_extra_keys(doc: dict[str, Any], allowed: frozenset[str], *, where: str) -> None:
    extra = sorted(set(doc) - allowed)
    if extra:
        raise ModuleValidationError(f"{where} carries unknown key(s) {extra}")


def _parse_parameters(raw: Any) -> dict[str, ModuleParameter]:
    _require_type(raw, dict, where="parameters")
    out: dict[str, ModuleParameter] = {}
    for name, spec in raw.items():
        where = f"parameter {name!r}"
        _require_type(spec, dict, where=where)
        _reject_section_extra_keys(spec, frozenset({"schema", "default"}), where=where)
        schema = _require_type(spec.get("schema"), dict, where=f"{where} schema")
        if "default" in spec:
            out[name] = ModuleParameter(schema=schema, has_default=True, default=spec["default"])
        else:
            out[name] = ModuleParameter(schema=schema, has_default=False)
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
                raise ModuleValidationError(f"{where} path segment {seg!r} must be a non-empty string ('*' or a key)")
        regime = entry.get("regime")
        if regime not in REGIMES:
            raise ModuleValidationError(f"{where} regime {regime!r} must be one of {sorted(REGIMES)}")
        rules.append(RegimeRule(path=list(path), regime=regime))
    return rules


def _parse_declarations(raw: Any) -> ModuleDeclarations:
    _require_type(raw, dict, where="declarations")
    _reject_section_extra_keys(raw, frozenset({"schema", "check"}), where="declarations")
    schema = _require_type(raw.get("schema"), dict, where="declarations schema")
    check = raw.get("check")
    if check is not None:
        _require_type(check, str, where="declarations check")
        if not check.strip():
            raise ModuleValidationError("declarations check must be a non-empty jq predicate or omitted")
        _compile_jq(check, where="declarations check")
    return ModuleDeclarations(schema=schema, check=check)


def _parse_trace(raw: Any) -> ModuleTrace:
    _require_type(raw, dict, where="trace")
    _reject_section_extra_keys(raw, frozenset({"enabled"}), where="trace")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ModuleValidationError(f"trace enabled must be a boolean, got {enabled!r}")
    return ModuleTrace(enabled=enabled)


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
        raise ModuleValidationError(f"module {name!r} schema fragment is not a valid object schema: {exc}") from exc


def validate_module(doc: Any) -> StateModule:
    """Parse and validate a raw platform state-module document, returning the
    :class:`StateModule`.

    Enforces every platform rule: ``kind == "state-module"``; the ``name`` form; the
    fragment is an object schema passing ``_validate_schema`` after defaults substitution
    (a no-default parameter must appear as a marker, and every marker names a declared
    parameter); regime paths lie inside the fragment (``"*"`` only over
    ``items``/``additionalProperties``); the declarations schema is an object and its
    optional ``check`` compiles. Any key outside the platform set is refused. Raises
    :class:`~tai42_contract.states.errors.ModuleValidationError` on the first violation."""
    _require_type(doc, dict, where="module document")
    _reject_extra_keys(doc, where="module document")

    if doc.get("kind") != MODULE_KIND:
        raise ModuleValidationError(f"module kind must be {MODULE_KIND!r}, got {doc.get('kind')!r}")

    name = _require_type(doc.get("name"), str, where="name")
    if not MODULE_NAME_RE.fullmatch(name):
        raise ModuleValidationError(f"module name {name!r} must match {MODULE_NAME_RE.pattern}")

    description = _require_type(doc.get("description", ""), str, where="description")
    parameters = _parse_parameters(doc.get("parameters", {}))
    schema = _require_type(doc.get("schema"), dict, where="schema")

    marker_names = set(_iter_marker_names(schema))
    unknown = sorted(marker_names - set(parameters))
    if unknown:
        raise ModuleValidationError(f"schema references undeclared parameter(s) {unknown}")
    for pname, param in parameters.items():
        if not param.has_default and pname not in marker_names:
            raise ModuleValidationError(
                f"parameter {pname!r} has no default, so it must appear as a $parameter marker in the fragment"
            )

    defaults_fragment = substitute_parameters(schema, {n: p.default for n, p in parameters.items() if p.has_default})
    _validate_fragment_schema(name, defaults_fragment)

    regimes = _parse_regimes(doc.get("regimes", []))
    for rule in regimes:
        _validate_regime_path(defaults_fragment, rule.path)

    declarations = _parse_declarations(doc["declarations"]) if "declarations" in doc else None
    trace = _parse_trace(doc.get("trace", {}))

    return StateModule(
        name=name,
        description=description,
        parameters=parameters,
        schema=schema,
        regimes=regimes,
        declarations=declarations,
        trace=trace,
    )


__all__ = [
    "MODULE_KIND",
    "REGIMES",
    "ModuleDeclarations",
    "ModuleParameter",
    "ModuleTrace",
    "RegimeRule",
    "StateModule",
    "compose_effective_schema",
    "path_overlaps",
    "regime_for",
    "substitute_parameters",
    "validate_module",
]
