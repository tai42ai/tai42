"""The recursive-descent grammar for the allowlisted condition language: the parser
that recognizes exactly the constructs the taint analysis can reason about and its
grammar vocabulary (keyword literals, operator precedence classes, builtin allowlist,
named refusals)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .budget import _Budget
from .errors import TokenFreeConditionError, _refusal
from .lexer import _lex
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
from .tokens import _describe, _Interpolation, _Kind, _Token

# Allowlisted builtins and their permitted arities. Each reads ONLY its input and
# arguments, which is what lets the taint rule treat a call as "input+args SAFE ⇒ result
# SAFE". Anything absent is refused; adding one is a security decision.
_BUILTIN_ARITIES: Mapping[str, frozenset[int]] = {
    "not": frozenset({0}),
    "length": frozenset({0}),
    "ascii_downcase": frozenset({0}),
    "startswith": frozenset({1}),
    "endswith": frozenset({1}),
    "contains": frozenset({1}),
    "test": frozenset({1, 2}),
    "IN": frozenset({1, 2}),
    "map": frozenset({1}),
    "any": frozenset({0, 1, 2}),
    "all": frozenset({0, 1, 2}),
}

# Names the allowlist already refuses; this table only makes the refusal say WHY.
_NAMED_REFUSALS: Mapping[str, str] = {
    "def": "defines a function, which can rename any builtin and so defeats the per-builtin rule",
    "env": "reads the process environment, which is not part of the auth context",
    "input": "reads a value from outside the auth context",
    "inputs": "reads values from outside the auth context",
    "now": "reads the wall clock, which is nondeterministic and outside the auth context; "
    "a fire reads the clock from .system.time",
    "localtime": "is outside the allowlisted builtins; express date arithmetic as a "
    "comparison against .system.*, which a fire can read",
    "gmtime": "is outside the allowlisted builtins; express date arithmetic as a "
    "comparison against .system.*, which a fire can read",
}
_NAMED_VARIABLE_REFUSALS: Mapping[str, str] = {
    "ENV": "reads the process environment, which is not part of the auth context",
    "__loc__": "reads the program's own source location, which is not part of the auth context",
}

_KEYWORD_LITERALS = frozenset({"true", "false", "null"})
_COMPARISONS = frozenset({"==", "!=", "<", "<=", ">", ">="})
_ADDITIVE = frozenset({"+", "-"})
_MULTIPLICATIVE = frozenset({"*", "/", "%"})


class _Parser:
    """A recursive-descent parser for the allowlisted condition grammar.

    It recognizes exactly the constructs the taint analysis can reason about and raises
    :class:`~tai42_skeleton.authz.token_free.errors.TokenFreeConditionError` on everything
    else, so an unknown construct is refused before any question of information flow is
    asked. Precedence follows jq's, lowest first: ``|``, ``,``, ``//``, ``or``, ``and``,
    comparison, ``+``/``-``, ``*``/``/``/``%``, unary, then postfix suffixes on a term.
    """

    def __init__(self, condition_text: str, tokens: Sequence[_Token], budget: _Budget) -> None:
        self._text = condition_text
        self._tokens = tokens
        self._index = 0
        self._budget = budget

    def parse(self) -> _Node:
        """The whole token stream as one expression; trailing tokens are a refusal."""
        node = self._pipe()
        token = self._peek()
        if token.kind is not _Kind.END:
            raise self._refuse(token, f"condition does not parse from {_describe(token)}")
        return node

    def _peek(self) -> _Token:
        return self._tokens[self._index]

    def _advance(self) -> _Token:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _at_operator(self, *texts: str) -> bool:
        token = self._peek()
        return token.kind is _Kind.OPERATOR and token.text in texts

    def _at_keyword(self, keyword: str) -> bool:
        token = self._peek()
        return token.kind is _Kind.IDENT and token.text == keyword

    def _expect_operator(self, text: str) -> _Token:
        if not self._at_operator(text):
            token = self._peek()
            raise self._refuse(token, f"condition is missing the {text!r} expected before {_describe(token)}")
        return self._advance()

    def _refuse(self, token: _Token, detail: str) -> TokenFreeConditionError:
        return _refusal(self._text, token.position, detail)

    def _descend(self) -> None:
        """Enter one level of nesting on the shared budget.

        Paired with a ``finally`` that leaves the level again. The three self-recursive
        rules — the pipe, the alternative and the unary prefixes — call it, and every
        other nested construct (parentheses, brackets, object values, ``as`` bodies)
        re-enters the grammar through the pipe, so one bound covers them all."""
        self._budget.descend(self._text, self._peek().position)

    def _pipe(self) -> _Node:
        self._descend()
        try:
            left = self._comma()
            if self._at_operator("|"):
                token = self._advance()
                return _Binary(token.position, "|", left, self._pipe())
            return left
        finally:
            self._budget.ascend()

    def _comma(self) -> _Node:
        left = self._alternative()
        while self._at_operator(","):
            token = self._advance()
            left = _Binary(token.position, ",", left, self._alternative())
        return left

    def _alternative(self) -> _Node:
        self._descend()
        try:
            left = self._disjunction()
            if self._at_operator("//"):
                token = self._advance()
                return _Binary(token.position, "//", left, self._alternative())
            return left
        finally:
            self._budget.ascend()

    def _disjunction(self) -> _Node:
        left = self._conjunction()
        while self._at_keyword("or"):
            token = self._advance()
            left = _Binary(token.position, "or", left, self._conjunction())
        return left

    def _conjunction(self) -> _Node:
        left = self._comparison()
        while self._at_keyword("and"):
            token = self._advance()
            left = _Binary(token.position, "and", left, self._comparison())
        return left

    def _comparison(self) -> _Node:
        left = self._additive()
        if self._at_operator(*_COMPARISONS):
            token = self._advance()
            return _Binary(token.position, token.text, left, self._additive())
        return left

    def _additive(self) -> _Node:
        left = self._multiplicative()
        while self._at_operator(*_ADDITIVE):
            token = self._advance()
            left = _Binary(token.position, token.text, left, self._multiplicative())
        return left

    def _multiplicative(self) -> _Node:
        left = self._unary()
        while self._at_operator(*_MULTIPLICATIVE):
            token = self._advance()
            left = _Binary(token.position, token.text, left, self._unary())
        return left

    def _unary(self) -> _Node:
        self._descend()
        try:
            if self._at_operator("-"):
                token = self._advance()
                return _Negate(token.position, self._unary())
            if self._at_keyword("try"):
                token = self._advance()
                body = self._unary()
                handler = None
                if self._at_keyword("catch"):
                    self._advance()
                    handler = self._unary()
                return _Try(token.position, body, handler)
            return self._postfix()
        finally:
            self._budget.ascend()

    def _postfix(self) -> _Node:
        start = self._peek()
        term = self._primary()
        suffixes: list[_Field | _Index | _Optional] = []
        while True:
            token = self._peek()
            if token.kind is _Kind.FIELD:
                self._advance()
                suffixes.append(_Field(token.position, token.text))
                continue
            if token.kind is _Kind.OPERATOR and token.text == "[":
                self._advance()
                if self._at_operator("]"):
                    self._advance()
                    suffixes.append(_Index(token.position, None))
                    continue
                key = self._pipe()
                self._expect_operator("]")
                suffixes.append(_Index(token.position, key))
                continue
            if token.kind is _Kind.OPERATOR and token.text == "?":
                self._advance()
                suffixes.append(_Optional(token.position))
                continue
            break
        node = _Projection(start.position, term, tuple(suffixes)) if suffixes else term
        if self._at_keyword("as"):
            token = self._advance()
            variable = self._peek()
            if variable.kind is not _Kind.VARIABLE:
                raise self._refuse(variable, "condition binds to something other than a plain variable name")
            self._advance()
            self._expect_operator("|")
            return _Bind(token.position, node, variable.text, self._pipe())
        return node

    def _primary(self) -> _Node:
        token = self._peek()
        if token.kind is _Kind.FIELD:
            # The suffix loop consumes the field itself; a leading field is a projection
            # of the value flowing in.
            return _Root(token.position)
        if token.kind is _Kind.STRING:
            self._advance()
            return _String(
                token.position, token.text, tuple(self._interpolation(part) for part in token.interpolations)
            )
        if token.kind is _Kind.NUMBER:
            self._advance()
            return _Constant(token.position)
        if token.kind is _Kind.VARIABLE:
            self._advance()
            named = _NAMED_VARIABLE_REFUSALS.get(token.text)
            if named is not None:
                raise self._refuse(token, f"condition uses '${token.text}', which {named}")
            return _Variable(token.position, token.text)
        if token.kind is _Kind.FORMAT:
            raise self._refuse(
                token, f"condition uses the format string {token.text!r}, which serializes its whole input"
            )
        if token.kind is _Kind.IDENT:
            return self._identifier()
        if token.kind is _Kind.OPERATOR:
            return self._operator_term()
        raise self._refuse(token, "condition ends before the expression is complete")

    def _operator_term(self) -> _Node:
        token = self._peek()
        if token.text == ".":
            self._advance()
            return _Root(token.position)
        if token.text == "..":
            raise self._refuse(
                token, "condition uses recursive descent '..', which reads every value in the auth context"
            )
        if token.text == "(":
            self._advance()
            node = self._pipe()
            self._expect_operator(")")
            return node
        if token.text == "[":
            self._advance()
            if self._at_operator("]"):
                self._advance()
                return _ArrayConstruction(token.position, None)
            element = self._pipe()
            self._expect_operator("]")
            return _ArrayConstruction(token.position, element)
        if token.text == "{":
            return self._object()
        raise self._refuse(token, f"condition uses the unsupported operator {token.text!r}")

    def _object(self) -> _Node:
        token = self._expect_operator("{")
        values: list[_Node] = []
        if not self._at_operator("}"):
            while True:
                key = self._peek()
                is_static_key = key.kind is _Kind.IDENT or (key.kind is _Kind.STRING and not key.interpolations)
                if not is_static_key:
                    raise self._refuse(key, "condition builds an object under a key that is not a static name")
                self._advance()
                self._expect_operator(":")
                values.append(self._alternative())
                if not self._at_operator(","):
                    break
                self._advance()
        self._expect_operator("}")
        return _ObjectConstruction(token.position, tuple(values))

    def _identifier(self) -> _Node:
        token = self._advance()
        if token.text in _KEYWORD_LITERALS:
            return _Constant(token.position)
        named = _NAMED_REFUSALS.get(token.text)
        if named is not None:
            raise self._refuse(token, f"condition uses {token.text!r}, which {named}")
        arguments: list[_Node] = []
        if self._at_operator("("):
            self._advance()
            while True:
                arguments.append(self._pipe())
                if not self._at_operator(";"):
                    break
                self._advance()
            self._expect_operator(")")
        arities = _BUILTIN_ARITIES.get(token.text)
        if arities is None:
            raise self._refuse(
                token,
                f"condition uses the builtin {token.text!r}, which is outside the set a background execution can be "
                "authorized against",
            )
        if len(arguments) not in arities:
            raise self._refuse(token, f"condition calls the builtin {token.text!r} with {len(arguments)} arguments")
        return _Call(token.position, token.text, tuple(arguments))

    def _interpolation(self, interpolation: _Interpolation) -> _Node:
        """The body of a ``\\(...)`` hole, parsed as the jq code it is — under the
        ENCLOSING scan's budget, so its tokens are spent from the same allowance and its
        nesting continues from the depth the hole sits at."""
        self._budget.descend(self._text, interpolation.start)
        try:
            tokens = _lex(self._text, self._budget, interpolation.start, interpolation.end)
            return _Parser(self._text, tokens, self._budget).parse()
        finally:
            self._budget.ascend()
