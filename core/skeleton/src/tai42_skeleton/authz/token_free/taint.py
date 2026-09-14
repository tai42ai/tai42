"""The information-flow rule over a parsed condition: one rule deciding that a ROOT
value (the whole auth context) may only be projected by a static field name, and the
per-node-kind handlers that enforce it."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from tai42_contract.access_control import OWNER_USER_ID_CLAIM

from .errors import _refusal
from .nodes import (
    _ArrayConstruction,
    _Binary,
    _Bind,
    _Call,
    _Constant,
    _Field,
    _Index,
    _Negate,
    _Node,
    _ObjectConstruction,
    _Optional,
    _Projection,
    _Root,
    _String,
    _Try,
    _Variable,
)

# The one context field whose content a fire cannot reproduce, and the one claim inside
# it that IS readable at a fire (from the execution key's stored policy data).
_IDENTITY_FIELD = "identity"
_OWNER_REFERENCE = f".{_IDENTITY_FIELD}.{OWNER_USER_ID_CLAIM}"


class _Taint(Enum):
    """What a value may carry.

    ``ROOT`` is the whole auth context — the value whose ``identity`` field a fire
    cannot present. ``SAFE`` is a value derived from it in a way that provably cannot
    depend on those claims.
    """

    ROOT = "root"
    SAFE = "safe"


@dataclass(frozen=True)
class _Scope:
    """The taint environment an expression is analyzed in: what ``.`` carries, and what
    each ``as``-bound variable carries."""

    subject: _Taint
    variables: Mapping[str, _Taint] = field(default_factory=dict)


class _TaintAnalysis:
    """The information-flow rule over a parsed condition.

    One rule decides the whole property: a ``ROOT`` value may only be PROJECTED by a
    static field name. Reaching a builtin, a comparison, an arithmetic operator, a
    constructor, an index expression, a string interpolation or the condition's own
    result is refused, because each of those turns the context into a value the
    condition's outcome depends on — and a fire cannot reproduce that context.
    """

    def __init__(self, condition_text: str) -> None:
        self._text = condition_text
        # node type → the flow rule for that kind; built once so :meth:`_taint` is a
        # lookup rather than an isinstance ladder.
        self._handlers: Mapping[type[_Node], Callable[[_Node, _Scope], _Taint]] = {
            _Root: self._taint_root,
            _Constant: self._taint_constant,
            _String: self._taint_string,
            _Variable: self._taint_variable,
            _Projection: self._projection,
            _Call: self._call,
            _Binary: self._binary,
            _Negate: self._taint_negate,
            _Bind: self._taint_bind,
            _Try: self._try,
            _ObjectConstruction: self._taint_object,
            _ArrayConstruction: self._taint_array,
        }

    def assert_safe(self, node: _Node) -> None:
        """Assert that the condition's RESULT does not depend on the identity claims."""
        self._require_safe(self._taint(node, _Scope(_Taint.ROOT)), node.position, "the condition's result")

    def _require_safe(self, taint: _Taint, position: int, consumer: str) -> None:
        if taint is _Taint.ROOT:
            raise _refusal(
                self._text,
                position,
                f"condition lets the whole auth context reach {consumer}; a background execution presents no token, "
                f"so the context may only be read through a statically named field (and {_OWNER_REFERENCE!r} is the "
                "only readable identity claim)",
            )

    def _taint(self, node: _Node, scope: _Scope) -> _Taint:
        handler = self._handlers.get(type(node))
        if handler is None:
            raise _refusal(self._text, node.position, "condition uses a construct the token-free scan cannot decide")
        return handler(node, scope)

    def _taint_root(self, node: _Node, scope: _Scope) -> _Taint:
        return scope.subject

    def _taint_constant(self, node: _Node, scope: _Scope) -> _Taint:
        return _Taint.SAFE

    def _taint_string(self, node: _Node, scope: _Scope) -> _Taint:
        assert isinstance(node, _String)
        for part in node.interpolations:
            self._require_safe(self._taint(part, scope), part.position, "a string interpolation")
        return _Taint.SAFE

    def _taint_variable(self, node: _Node, scope: _Scope) -> _Taint:
        assert isinstance(node, _Variable)
        taint = scope.variables.get(node.name)
        if taint is None:
            raise _refusal(self._text, node.position, f"condition reads the unbound variable '${node.name}'")
        return taint

    def _taint_negate(self, node: _Node, scope: _Scope) -> _Taint:
        assert isinstance(node, _Negate)
        self._require_safe(self._taint(node.operand, scope), node.position, "arithmetic negation")
        return _Taint.SAFE

    def _taint_bind(self, node: _Node, scope: _Scope) -> _Taint:
        assert isinstance(node, _Bind)
        variables = {**scope.variables, node.variable: self._taint(node.source, scope)}
        return self._taint(node.body, _Scope(scope.subject, variables))

    def _taint_object(self, node: _Node, scope: _Scope) -> _Taint:
        assert isinstance(node, _ObjectConstruction)
        for value in node.values:
            self._require_safe(self._taint(value, scope), value.position, "an object being constructed")
        return _Taint.SAFE

    def _taint_array(self, node: _Node, scope: _Scope) -> _Taint:
        assert isinstance(node, _ArrayConstruction)
        if node.element is not None:
            self._require_safe(self._taint(node.element, scope), node.element.position, "an array being collected")
        return _Taint.SAFE

    def _projection(self, node: _Node, scope: _Scope) -> _Taint:
        """The taint of a suffix chain — the ONE place a ``ROOT`` value is allowed to be
        consumed, and only by a static field name."""
        assert isinstance(node, _Projection)
        taint = self._taint(node.source, scope)
        suffixes = node.suffixes
        position = 0
        while position < len(suffixes):
            suffix = suffixes[position]
            if taint is _Taint.SAFE:
                # Every field of a safe value is safe, and so is every element of it —
                # but WHICH element is selected must not depend on the context either.
                if isinstance(suffix, _Index) and suffix.key is not None:
                    self._require_safe(self._taint(suffix.key, scope), suffix.position, "an index expression")
                position += 1
                continue
            name = _static_name(suffix)
            if name is None:
                raise _refusal(
                    self._text,
                    suffix.position,
                    "condition reaches into the auth context without naming a field; only a statically named field "
                    "can be read at a fire",
                )
            if name == _IDENTITY_FIELD:
                remaining = suffixes[position + 1 :]
                if len(remaining) != 1 or _static_name(remaining[0]) != OWNER_USER_ID_CLAIM:
                    raise _refusal(
                        self._text,
                        suffix.position,
                        f"condition reads an identity claim beyond {_OWNER_REFERENCE!r}, which is the only one a "
                        "background execution can present",
                    )
                return _Taint.SAFE
            taint = _Taint.SAFE
            position += 1
        return taint

    def _call(self, node: _Node, scope: _Scope) -> _Taint:
        """A builtin reads its input and its arguments, so both must be safe — and its
        result is then derived from safe values only."""
        assert isinstance(node, _Call)
        self._require_safe(scope.subject, node.position, f"the builtin {node.name!r}")
        # jq evaluates every argument against the call's own input, and the filter
        # arguments of ``map``/``any``/``all`` against an element of it. That input is
        # safe by the check above, so each argument is analyzed under a safe subject.
        arguments = _Scope(_Taint.SAFE, scope.variables)
        for argument in node.arguments:
            self._require_safe(
                self._taint(argument, arguments), argument.position, f"an argument of the builtin {node.name!r}"
            )
        return _Taint.SAFE

    def _binary(self, node: _Node, scope: _Scope) -> _Taint:
        assert isinstance(node, _Binary)
        if node.operator == "|":
            piped = _Scope(self._taint(node.left, scope), scope.variables)
            return self._taint(node.right, piped)
        if node.operator in (",", "//"):
            # Both sides can reach the output, so the stream carries the taint of either.
            left = self._taint(node.left, scope)
            right = self._taint(node.right, scope)
            return _Taint.SAFE if left is _Taint.SAFE and right is _Taint.SAFE else _Taint.ROOT
        consumer = f"the {node.operator!r} operator"
        self._require_safe(self._taint(node.left, scope), node.left.position, consumer)
        self._require_safe(self._taint(node.right, scope), node.right.position, consumer)
        return _Taint.SAFE

    def _try(self, node: _Node, scope: _Scope) -> _Taint:
        assert isinstance(node, _Try)
        body = self._taint(node.body, scope)
        if node.handler is None:
            return body
        # A jq error message quotes the values that produced it, so the handler's input
        # is treated as the context itself.
        handler = self._taint(node.handler, _Scope(_Taint.ROOT, scope.variables))
        return _Taint.SAFE if body is _Taint.SAFE and handler is _Taint.SAFE else _Taint.ROOT


def _static_name(suffix: _Field | _Index | _Optional) -> str | None:
    """The field name a suffix statically projects, or ``None`` when it projects
    something only evaluation could name (``[]``, a computed or interpolated index, an
    error-suppressing ``?``)."""
    if isinstance(suffix, _Field):
        return suffix.name
    if isinstance(suffix, _Index) and isinstance(suffix.key, _String) and not suffix.key.interpolations:
        return suffix.key.value
    return None
