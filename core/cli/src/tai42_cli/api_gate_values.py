"""The API-diff gate's attribute-value rule: which changed public attribute values are breaking.

The rule walks the same public-member tree ``griffe`` walks for its own breakages and
compares each public attribute's bound value expression on the two sides. A changed
value is breaking unless one of the exceptions below accepts it, each a purely
structural comparison of the two expressions (parsed, never evaluated) that fails
closed — any doubt keeps the change breaking:

  * the attribute is bound to a ``logging.getLogger(...)`` call (a module's own log
    channel, never API surface);
  * a collection constant only gained entries, or a ``Literal[...]`` alias only
    gained members;
  * the change is confined to the documentation keywords of a known
    decorator/constructor call (a pydantic ``Field``, a ``typer``
    ``Typer``/``Option``/``Argument``), with ``json_schema_extra`` forgiven
    key by key.

``griffe`` (the ``api-gate`` extra) is imported lazily inside the walk, so the pure
expression helpers import and unit-test in an environment that does not carry it.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    import griffe

# Known decorator/constructor callee name -> its keywords that carry documentation
# only; a change confined to those keywords is metadata, not API surface.
_DOC_KEYWORDS: dict[str, frozenset[str]] = {
    "Field": frozenset({"description", "title", "examples"}),
    "Typer": frozenset({"help", "epilog", "rich_help_panel", "short_help"}),
    "Option": frozenset({"help", "rich_help_panel"}),
    "Argument": frozenset({"help", "rich_help_panel"}),
}
# The pydantic ``json_schema_extra`` mapping mixes documentation with behaviour:
# alongside doc keys it carries ``secret`` (redaction), ``reload`` (settings reload
# disposition), ``key_material``, schema-shaping keys like ``type`` and more. A
# change to it is therefore forgiven only per key, and only for keys whose meaning
# is purely documentation; every other key — known-behavioural or simply unknown —
# keeps gating (fail closed). It is owned by ``Field`` only.
_SCHEMA_EXTRA_KEYWORD = "json_schema_extra"
_SCHEMA_EXTRA_CALLEES = frozenset({"Field"})
_SCHEMA_EXTRA_DOC_KEYS = frozenset({"x-tai42-expression", "description", "title", "examples", "deprecated"})
# The breakage kind text of the gate's own attribute-value rule.
_VALUE_CHANGED = "Attribute value was changed"


def _call_callee_name(func: ast.expr) -> str | None:
    """Callee name of a call node, else ``None``.

    A bare name (``Field``) or a dotted attribute (``pydantic.Field`` -> ``Field``)
    yields the final name; any other expression yields ``None``.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _pop_kwarg(node: ast.Call, name: str) -> ast.expr | None:
    """Remove and return the value of keyword ``name`` from ``node``, or ``None`` when absent.

    Mutates ``node.keywords``. A ``**expansion`` (``kw.arg is None``) is never matched and is
    left in place, so a spread ``json_schema_extra`` survives into the structural comparison
    and, if it differs at all, keeps the change breaking.
    """
    for i, kw in enumerate(node.keywords):
        if kw.arg == name:
            del node.keywords[i]
            return kw.value
    return None


def _literal_str_key_dict(node: ast.expr) -> dict[str, str] | None:
    """Map each literal-string key to ``ast.dump`` of its value, else ``None``.

    Returns the mapping only when ``node`` is an ``ast.Dict`` whose keys are ALL literal
    strings; a ``**expansion`` (a ``None`` key node) or a non-string/duplicate key makes the
    mapping non-literal and returns ``None`` so the caller fails closed.
    """
    if not isinstance(node, ast.Dict):
        return None
    result: dict[str, str] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        if key.value in result:
            return None
        result[key.value] = ast.dump(value)
    return result


def _schema_extra_delta_is_doc_only(old_value: ast.expr | None, new_value: ast.expr | None) -> bool:
    """True when a ``json_schema_extra`` change is confined to documentation keys.

    Both values must be literal string-keyed dicts (an absent side, ``None``, counts
    as the empty dict); the change is forgiven only when every key whose dumped value
    differs between the two — added, removed or edited — is in
    :data:`_SCHEMA_EXTRA_DOC_KEYS`. A non-dict/callable value, a non-literal key, or
    any delta touching a behavioural or unknown key returns ``False`` (fail closed).
    """
    old_map = {} if old_value is None else _literal_str_key_dict(old_value)
    new_map = {} if new_value is None else _literal_str_key_dict(new_value)
    if old_map is None or new_map is None:
        return False
    for key in old_map.keys() | new_map.keys():
        if old_map.get(key) != new_map.get(key) and key not in _SCHEMA_EXTRA_DOC_KEYS:
            return False
    return True


def _is_literal_to_doc_field(old_node: ast.expr, new_node: ast.Call) -> bool:
    """True when a plain-literal default was merely wrapped into a known callee's ``default=``.

    The wrapping call's ONLY non-documentation content must be a ``default=`` equal to that
    same literal — e.g. ``None`` -> ``Field(default=None, json_schema_extra={...})``. The
    default must be passed by keyword and be structurally identical to the old literal; any
    positional argument, any missing/differing default, or any non-doc keyword
    (``alias``/``ge``/…) makes it real surface and returns ``False``. A wrapped
    ``json_schema_extra`` payload is allowed only when its delta versus the empty mapping is
    documentation-only (see :func:`_schema_extra_delta_is_doc_only`).
    """
    callee = _call_callee_name(new_node.func)
    if callee not in _SCHEMA_EXTRA_CALLEES or new_node.args:
        return False
    allowed_doc = _DOC_KEYWORDS[callee]
    default_matches = False
    for kw in new_node.keywords:
        if kw.arg == "default":
            if ast.dump(kw.value) != ast.dump(old_node):
                return False
            default_matches = True
        elif kw.arg == _SCHEMA_EXTRA_KEYWORD:
            if not _schema_extra_delta_is_doc_only(None, kw.value):
                return False
        elif kw.arg not in allowed_doc:
            return False
    return default_matches


def _is_doc_only_call_change(old_expr: str, new_expr: str) -> bool:
    """True only when a value change is confined to a known decorator/constructor callee's documentation metadata.

    Two shapes qualify:

    * call -> call to the SAME known callee (a key of ``_DOC_KEYWORDS``) that is
      structurally equal after dropping that callee's documentation-only keywords.
      For an owning callee, ``json_schema_extra`` is compared separately, key by
      key: the change is forgiven only when its delta touches documentation keys
      alone (see :func:`_schema_extra_delta_is_doc_only`), so flipping a behavioural
      key — and any change that also touches a non-doc keyword — stays breaking.
    * literal -> call that merely wraps the old literal as ``default=`` on a known
      callee with only documentation keywords besides it (see
      :func:`_is_literal_to_doc_field`).

    A parse failure, a non-call new expression, an unknown callee or a callee
    mismatch returns ``False`` — the classification stays breaking — and any other
    surprise propagates to the caller as a loud crash.
    """
    try:
        old_node = ast.parse(old_expr, mode="eval").body
        new_node = ast.parse(new_expr, mode="eval").body
    except (SyntaxError, ValueError):
        return False
    if isinstance(new_node, ast.Call) and not isinstance(old_node, ast.Call):
        return _is_literal_to_doc_field(old_node, new_node)
    if not (isinstance(old_node, ast.Call) and isinstance(new_node, ast.Call)):
        return False
    callee = _call_callee_name(old_node.func)
    if callee is None or callee != _call_callee_name(new_node.func):
        return False
    doc_keywords = _DOC_KEYWORDS.get(callee)
    if doc_keywords is None:
        return False
    if callee in _SCHEMA_EXTRA_CALLEES:
        old_extra = _pop_kwarg(old_node, _SCHEMA_EXTRA_KEYWORD)
        new_extra = _pop_kwarg(new_node, _SCHEMA_EXTRA_KEYWORD)
        if (old_extra is not None or new_extra is not None) and not _schema_extra_delta_is_doc_only(
            old_extra, new_extra
        ):
            return False
    old_node.keywords = [kw for kw in old_node.keywords if kw.arg not in doc_keywords]
    new_node.keywords = [kw for kw in new_node.keywords if kw.arg not in doc_keywords]
    return ast.dump(old_node) == ast.dump(new_node)


# Collection wrappers whose single positional argument holds the underlying
# literal, so ``MappingProxyType({...})`` and ``frozenset({...})`` compare as the
# dict/set they wrap. A change of wrapper (``frozenset`` -> ``set``) is NOT a
# match: both sides must unwrap through an identical wrapper-name chain.
_COLLECTION_WRAPPERS = frozenset({"MappingProxyType", "frozenset", "tuple", "list", "set", "dict"})


def _unwrap_collection(node: ast.expr) -> tuple[ast.expr, tuple[str, ...]]:
    """Peel any chain of recognised single-argument collection wrappers off ``node``.

    Returns the innermost expression and the wrapper-name chain that was peeled (outermost
    first). A wrapper only unwraps when its callee is a bare or dotted name in
    :data:`_COLLECTION_WRAPPERS` with EXACTLY one positional argument and NO keywords;
    anything else stops the peeling.
    """
    chain: list[str] = []
    while (
        isinstance(node, ast.Call)
        and _call_callee_name(node.func) in _COLLECTION_WRAPPERS
        and len(node.args) == 1
        and not node.keywords
        and not isinstance(node.args[0], ast.Starred)
    ):
        name = _call_callee_name(node.func)
        if name is None:
            raise AssertionError
        chain.append(name)
        node = node.args[0]
    return node, tuple(chain)


def _is_subsequence(old: list[str], new: list[str]) -> bool:
    """True when every element of ``old`` appears in ``new`` in the same relative order.

    Insertions anywhere are allowed, reorderings are not.
    """
    it = iter(new)
    return all(elem in it for elem in old)


def _dict_is_additive(old_inner: ast.Dict, new_inner: ast.Dict) -> bool:
    """True when ``new_inner`` is a strict superset of ``old_inner``.

    Every old key is preserved with an unchanged value and at least one key was added. A
    ``**`` expansion (a ``None`` key node) or a duplicate key makes the mapping ambiguous and
    fails closed.
    """
    old_pairs = [
        (ast.dump(k), ast.dump(v)) for k, v in zip(old_inner.keys, old_inner.values, strict=True) if k is not None
    ]
    new_pairs = [
        (ast.dump(k), ast.dump(v)) for k, v in zip(new_inner.keys, new_inner.values, strict=True) if k is not None
    ]
    if len(old_pairs) != len(old_inner.keys) or len(new_pairs) != len(new_inner.keys):
        return False  # a ``**expansion`` has a None key node — not a plain literal
    old_keys = [k for k, _ in old_pairs]
    new_keys = [k for k, _ in new_pairs]
    if len(set(old_keys)) != len(old_keys) or len(set(new_keys)) != len(new_keys):
        return False  # a duplicate key dump makes the mapping ambiguous
    new_by_key = dict(new_pairs)
    for key, value in old_pairs:
        if new_by_key.get(key) != value:
            return False  # a missing key or a changed value for a preserved key
    return len(new_pairs) > len(old_pairs)


def _set_is_additive(old_inner: ast.Set, new_inner: ast.Set) -> bool:
    """True when ``new_inner`` is a strict multiset-superset of ``old_inner`` and grew.

    A ``*`` unpack element fails closed.
    """
    if any(isinstance(e, ast.Starred) for e in (*old_inner.elts, *new_inner.elts)):
        return False
    old_dumps = [ast.dump(e) for e in old_inner.elts]
    new_dumps = [ast.dump(e) for e in new_inner.elts]
    return Counter(old_dumps) <= Counter(new_dumps) and len(new_dumps) > len(old_dumps)


def _sequence_is_additive(old_inner: ast.Tuple | ast.List, new_inner: ast.Tuple | ast.List) -> bool:
    """True when the new tuple/list grew and keeps the old elements as an ordered subsequence.

    A ``*`` unpack element fails closed.
    """
    if any(isinstance(e, ast.Starred) for e in (*old_inner.elts, *new_inner.elts)):
        return False
    old_dumps = [ast.dump(e) for e in old_inner.elts]
    new_dumps = [ast.dump(e) for e in new_inner.elts]
    return len(new_dumps) > len(old_dumps) and _is_subsequence(old_dumps, new_dumps)


def _is_additive_collection_growth(old_expr: str, new_expr: str) -> bool:
    """True only when both values are same-typed collection constants and the new is a strict superset.

    The growth must be purely additive (dict/set gained entries, tuple/list gained elements
    without disturbing the order of the pre-existing ones). Every other relation — element
    removal or edit, a changed value for a preserved dict key, a reorder, a wrapper-type or
    collection-type change, a duplicate dict key, a non-collection expression, or an identical
    collection — returns ``False`` so the classification stays breaking. The comparison is
    purely structural (parse-only, never evaluated); a parse failure or any ambiguity fails
    closed to ``False``.
    """
    try:
        old_node = ast.parse(old_expr, mode="eval").body
        new_node = ast.parse(new_expr, mode="eval").body
    except (SyntaxError, ValueError):
        return False
    old_inner, old_chain = _unwrap_collection(old_node)
    new_inner, new_chain = _unwrap_collection(new_node)
    if old_chain != new_chain:
        return False
    if type(old_inner) is not type(new_inner):
        return False
    if isinstance(old_inner, ast.Dict) and isinstance(new_inner, ast.Dict):
        return _dict_is_additive(old_inner, new_inner)
    if isinstance(old_inner, ast.Set) and isinstance(new_inner, ast.Set):
        return _set_is_additive(old_inner, new_inner)
    if isinstance(old_inner, ast.Tuple | ast.List) and isinstance(new_inner, ast.Tuple | ast.List):
        return _sequence_is_additive(old_inner, new_inner)
    return False


def _literal_members(node: ast.expr) -> tuple[str, list[str]] | None:
    """A ``Literal[...]`` subscript's reference dump and its constant members, else ``None``.

    ``node`` qualifies only as an ``ast.Subscript`` whose value is a ``Literal`` /
    ``typing.Literal`` / ``t.Literal``-style name or attribute. The slice is normalised to a
    sequence of members — a single-member ``Literal['x']`` becomes a one-element list — and every
    member must be an ``ast.Constant``. A non-subscript, a non-``Literal`` reference, a non-constant
    member, or a starred element returns ``None`` so the caller fails closed. The reference dump is
    returned alongside the members so the caller can reject a spelling change
    (``Literal`` -> ``typing.Literal``).
    """
    if not isinstance(node, ast.Subscript):
        return None
    ref = node.value
    if isinstance(ref, ast.Name):
        if ref.id != "Literal":
            return None
    elif isinstance(ref, ast.Attribute):
        if ref.attr != "Literal":
            return None
    else:
        return None
    elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    members: list[str] = []
    for elt in elts:
        if not isinstance(elt, ast.Constant):
            return None
        members.append(ast.dump(elt))
    return ast.dump(ref), members


def _is_additive_literal_growth(old_expr: str, new_expr: str) -> bool:
    """True only when both values are ``Literal[...]`` aliases and the new adds members in order.

    Both expressions must be ``Literal[...]`` subscripts over the SAME reference spelling whose
    members are all constants; the new members must keep the old ones as a strict ordered
    subsequence (members appended or inserted, none removed, edited or reordered). Every other
    relation — a removal, an edit, a reorder, a wrapper/spelling change, ``Literal`` to
    non-``Literal`` or the reverse, a non-constant member, a non-subscript expression, or an
    identical alias — returns ``False`` so the classification stays breaking. The comparison is
    purely structural (parse-only, never evaluated); a parse failure fails closed to ``False``.
    """
    try:
        old_node = ast.parse(old_expr, mode="eval").body
        new_node = ast.parse(new_expr, mode="eval").body
    except (SyntaxError, ValueError):
        return False
    old = _literal_members(old_node)
    new = _literal_members(new_node)
    if old is None or new is None:
        return False
    old_ref, old_members = old
    new_ref, new_members = new
    if old_ref != new_ref:
        return False
    return len(new_members) > len(old_members) and _is_subsequence(old_members, new_members)


_LOGGER_BINDING = re.compile(r"^(logging\.)?getLogger\(")


def is_logger_binding(value: str | None) -> bool:
    """True when an attribute's bound expression is a ``logging.getLogger(...)`` call."""
    return value is not None and _LOGGER_BINDING.match(value.strip()) is not None


def _attribute_value_changes(
    old: griffe.Object | griffe.Alias,
    new: griffe.Object | griffe.Alias,
    seen: set[str] | None = None,
) -> Iterator[tuple[griffe.Attribute, str | griffe.Expr | None, str | griffe.Expr | None]]:
    """Yield ``(new attribute, old value, new value)`` for each public attribute whose value changed.

    The walk is the one ``griffe`` makes for its own breakages: every public member of
    ``old`` (inherited members included) that ``new`` still has is compared, modules and
    classes recursively; a member ``new`` no longer has is ``griffe``'s removal finding,
    not a value change. ``seen`` holds the paths already compared, so an alias and its
    target — a re-export, an inherited member — are compared once, at the target. A value
    is the bound expression (``None`` when the attribute is only annotated), compared
    structurally.
    """
    seen = set() if seen is None else seen
    new_members = new.all_members
    for name, old_member in old.all_members.items():
        if not old_member.is_public:
            continue
        new_member = new_members.get(name)
        if new_member is not None:
            yield from _member_value_changes(old_member, new_member, seen)


def _member_value_changes(
    old_member: griffe.Object | griffe.Alias,
    new_member: griffe.Object | griffe.Alias,
    seen: set[str],
) -> Iterator[tuple[griffe.Attribute, str | griffe.Expr | None, str | griffe.Expr | None]]:
    """Compare one member pair for :func:`_attribute_value_changes`.

    An alias side is replaced by its target first. An alias whose target cannot be
    resolved points outside the loaded packages (a third-party object), which is not
    this package's surface, so the pair is not compared — as ``griffe`` does for its own
    breakages. A pair whose kinds differ is ``griffe``'s kind-change finding.
    """
    import griffe

    if old_member.path in seen:
        return
    seen.add(old_member.path)
    if isinstance(old_member, griffe.Alias) or isinstance(new_member, griffe.Alias):
        try:
            old_target = old_member.target if isinstance(old_member, griffe.Alias) else old_member
            new_target = new_member.target if isinstance(new_member, griffe.Alias) else new_member
        except griffe.AliasResolutionError:
            return
        yield from _member_value_changes(old_target, new_target, seen)
    elif old_member.kind != new_member.kind:
        return
    elif old_member.is_module or old_member.is_class:
        yield from _attribute_value_changes(old_member, new_member, seen)
    elif (
        isinstance(old_member, griffe.Attribute)
        and isinstance(new_member, griffe.Attribute)
        and old_member.value != new_member.value
    ):
        yield new_member, old_member.value, new_member.value


def _explain_value_change(
    attribute: griffe.Attribute, old_value: str | griffe.Expr | None, new_value: str | griffe.Expr | None
) -> str:
    """One finding line for a changed attribute value, in ``griffe``'s one-line explanation shape.

    ``<file>:<line>: <path in its module>: Attribute value was changed: <old> -> <new>``,
    where the file is relative to the working directory when it lies under it, the path
    is the attribute's canonical path inside its own module, and a value that was removed
    (the attribute is now only annotated) reads ``unset``.
    """
    relative_path = attribute.canonical_path[len(attribute.module.path) + 1 :] or "<module>"
    new_text = "unset" if new_value is None else str(new_value)
    return (
        f"{attribute.relative_filepath}:{attribute.lineno or 0}: {relative_path}: "
        f"{_VALUE_CHANGED}: {old_value} -> {new_text}"
    )


def value_change_findings(old: griffe.Object | griffe.Alias, new: griffe.Object | griffe.Alias) -> list[str]:
    """The breaking attribute-value findings between two loaded versions of a module, one line each.

    Every change :func:`_attribute_value_changes` yields is a finding unless the new value
    is a logger binding or the change is additive collection growth, additive
    ``Literal`` growth or a documentation-only call change.
    """
    findings: list[str] = []
    for attribute, old_value, new_value in _attribute_value_changes(old, new):
        old_text, new_text = str(old_value), str(new_value)
        if (
            is_logger_binding(new_text)
            or _is_additive_collection_growth(old_text, new_text)
            or _is_additive_literal_growth(old_text, new_text)
            or _is_doc_only_call_change(old_text, new_text)
        ):
            continue
        findings.append(_explain_value_change(attribute, old_value, new_value))
    return findings
