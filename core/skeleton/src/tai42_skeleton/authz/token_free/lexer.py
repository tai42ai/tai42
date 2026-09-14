"""Tokenizing the condition text into the token stream the parser consumes: the token
loop, one scanner per lexical class, and the character-class vocabulary they read."""

from __future__ import annotations

from string import ascii_letters, digits

from .budget import _Budget
from .errors import _refusal
from .tokens import _Interpolation, _Kind, _Token

_IDENT_START = frozenset(ascii_letters + "_")
_IDENT_CHARS = _IDENT_START | frozenset(digits)
_DIGITS = frozenset(digits)

# The only token separator; every other whitespace character is already refused
# pre-lexically before a token is read — control whitespace by
# :func:`~tai42_skeleton.authz.token_free.source_shape._assert_no_control_characters`,
# the rest by
# :func:`~tai42_skeleton.authz.token_free.source_shape._assert_source_shape`.
_WHITESPACE = frozenset(" ")

# Punctuation, longest first so a prefix never shadows a longer operator.
_OPERATORS = (
    "?//",
    "//=",
    "|=",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "==",
    "!=",
    "<=",
    ">=",
    "//",
    "..",
    "=",
    "<",
    ">",
    "|",
    ",",
    "+",
    "-",
    "*",
    "/",
    "%",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    ":",
    ";",
    "?",
    ".",
)

# The escapes a string literal is decoded through. ``\uXXXX`` is deliberately absent and
# refused: it is the one escape that mints a character the source-shape gate never saw.
_STRING_ESCAPES: dict[str, str] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def _lex_string(condition_text: str, start: int, budget: _Budget) -> tuple[_Token, int]:
    """Lex the string literal opening at ``start``, returning it and the offset just
    past its closing quote. ``text`` is the DECODED literal value, meaningful only when
    the string carries no interpolation.

    Mutually recursive with :func:`_scan_interpolation` once per nested ``\\(...)`` hole,
    so it takes a level of ``budget``."""
    budget.descend(condition_text, start)
    try:
        parts: list[str] = []
        interpolations: list[_Interpolation] = []
        index = start + 1
        while index < len(condition_text):
            char = condition_text[index]
            if char == '"':
                return _Token(_Kind.STRING, "".join(parts), start, tuple(interpolations)), index + 1
            if char != "\\":
                parts.append(char)
                index += 1
                continue
            index += 1
            if index >= len(condition_text):
                break
            escape = condition_text[index]
            if escape == "(":
                body_start = index + 1
                body_end = _scan_interpolation(condition_text, body_start, budget)
                interpolations.append(_Interpolation(body_start, body_end))
                index = body_end + 1
                continue
            if escape not in _STRING_ESCAPES:
                raise _refusal(condition_text, index - 1, f"condition contains the unsupported escape '\\{escape}'")
            parts.append(_STRING_ESCAPES[escape])
            index += 1
        raise _refusal(condition_text, start, "condition contains an unterminated string literal")
    finally:
        budget.ascend()


def _scan_interpolation(condition_text: str, start: int, budget: _Budget) -> int:
    """The offset of the ``)`` closing the interpolation body that opens at ``start``.
    Nested strings are skipped exactly as the lexer skips them, so a parenthesis inside
    one never closes the body.

    Mutually recursive with :func:`_lex_string` over those nested strings, so it takes a
    level of ``budget`` too."""
    budget.descend(condition_text, start - 2)
    try:
        depth = 1
        index = start
        while index < len(condition_text):
            char = condition_text[index]
            if char == '"':
                _, index = _lex_string(condition_text, index, budget)
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        raise _refusal(condition_text, start - 2, "condition contains an unterminated string interpolation")
    finally:
        budget.ascend()


def _lex_variable(condition_text: str, index: int, stop: int) -> tuple[_Token, int]:
    """Scan a ``$name`` variable, refusing a nameless ``$``."""
    name_end = index + 1
    while name_end < stop and condition_text[name_end] in _IDENT_CHARS:
        name_end += 1
    if name_end == index + 1:
        raise _refusal(condition_text, index, "condition contains a nameless variable")
    return _Token(_Kind.VARIABLE, condition_text[index + 1 : name_end], index), name_end


def _lex_format(condition_text: str, index: int, stop: int) -> tuple[_Token, int]:
    """Scan an ``@name`` format token."""
    name_end = index + 1
    while name_end < stop and condition_text[name_end] in _IDENT_CHARS:
        name_end += 1
    return _Token(_Kind.FORMAT, condition_text[index:name_end], index), name_end


def _lex_identifier(condition_text: str, index: int, stop: int) -> tuple[_Token, int]:
    """Scan a bare identifier run."""
    name_end = index
    while name_end < stop and condition_text[name_end] in _IDENT_CHARS:
        name_end += 1
    return _Token(_Kind.IDENT, condition_text[index:name_end], index), name_end


def _is_field_start(condition_text: str, index: int, stop: int) -> bool:
    """Whether the ``.`` at ``index`` opens a ``.name`` or ``."name"`` field suffix
    (rather than the ``.`` / ``..`` operators)."""
    if index + 1 >= stop:
        return False
    following = condition_text[index + 1]
    return following in _IDENT_START or following == '"'


def _lex_field(condition_text: str, index: int, stop: int, budget: _Budget) -> tuple[_Token, int]:
    """Scan a ``.name`` or ``."name"`` field suffix, refusing an interpolated field name.
    Only called when :func:`_is_field_start` holds at ``index``."""
    if condition_text[index + 1] in _IDENT_START:
        name_end = index + 1
        while name_end < stop and condition_text[name_end] in _IDENT_CHARS:
            name_end += 1
        return _Token(_Kind.FIELD, condition_text[index + 1 : name_end], index), name_end
    quoted, next_index = _lex_string(condition_text, index + 1, budget)
    if quoted.interpolations:
        raise _refusal(condition_text, quoted.position, "condition names a field by an interpolated string")
    return _Token(_Kind.FIELD, quoted.text, quoted.position - 1), next_index


def _lex_operator(condition_text: str, index: int) -> tuple[_Token, int]:
    """Match the longest punctuation operator at the cursor, refusing an unrecognized
    character."""
    operator = next((candidate for candidate in _OPERATORS if condition_text.startswith(candidate, index)), None)
    if operator is None:
        raise _refusal(
            condition_text, index, f"condition contains the unrecognized character {condition_text[index]!r}"
        )
    return _Token(_Kind.OPERATOR, operator, index), index + len(operator)


def _lex_number(condition_text: str, start: int, stop: int) -> tuple[_Token, int]:
    """Lex the numeric literal at ``start``, returning it and the offset just past it."""
    index = start
    while index < stop and condition_text[index] in _DIGITS:
        index += 1
    if index < stop and condition_text[index] == "." and index + 1 < stop and condition_text[index + 1] in _DIGITS:
        index += 1
        while index < stop and condition_text[index] in _DIGITS:
            index += 1
    if index < stop and condition_text[index] in "eE":
        exponent = index + 1
        if exponent < stop and condition_text[exponent] in "+-":
            exponent += 1
        if exponent < stop and condition_text[exponent] in _DIGITS:
            while exponent < stop and condition_text[exponent] in _DIGITS:
                exponent += 1
            index = exponent
    return _Token(_Kind.NUMBER, condition_text[start:index], start), index


def _scan_token(condition_text: str, index: int, stop: int, budget: _Budget) -> tuple[_Token, int]:
    """Classify the character at ``index`` and delegate to the matching scanner,
    returning the token and the offset just past it."""
    char = condition_text[index]
    if char == '"':
        return _lex_string(condition_text, index, budget)
    if char == "$":
        return _lex_variable(condition_text, index, stop)
    if char == "@":
        return _lex_format(condition_text, index, stop)
    if char in _DIGITS:
        return _lex_number(condition_text, index, stop)
    if char in _IDENT_START:
        return _lex_identifier(condition_text, index, stop)
    if char == "." and _is_field_start(condition_text, index, stop):
        return _lex_field(condition_text, index, stop, budget)
    return _lex_operator(condition_text, index)


def _lex(condition_text: str, budget: _Budget, start: int = 0, end: int | None = None) -> list[_Token]:
    """Tokenize ``condition_text[start:end]``, with every token carrying its offset in
    the WHOLE condition so a refusal points at the real text. Every token emitted is
    spent from ``budget``, whether it sits in the outer text or inside an interpolation
    body."""
    stop = len(condition_text) if end is None else end
    tokens: list[_Token] = []
    index = start
    while index < stop:
        if condition_text[index] in _WHITESPACE:
            index += 1
            continue
        token, index = _scan_token(condition_text, index, stop, budget)
        budget.spend(condition_text, token.position)
        tokens.append(token)
    end_token = _Token(_Kind.END, "", stop)
    budget.spend(condition_text, end_token.position)
    tokens.append(end_token)
    return tokens
