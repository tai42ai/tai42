"""Representative-path derivation for dynamic route patterns.

A dynamic route pattern is a regex, non-enumerable into concrete paths. To jq-check
it a single concrete path that PROVABLY matches the pattern is derived from the regex
AST and validated with ``fullmatch`` before use — a pattern whose representative cannot
be derived is EXCLUDED (under-showing is safe; over-showing is the topology-leak bug),
never emitted unfiltered.

:func:`_sample_path_for_pattern` is the one entry point the projection build consumes;
the ``_emit_*`` opcode samplers and their helpers walk the parsed regex AST, each
producing one matching character or run for its opcode (or ``None`` when no concrete
sample can be safely derived).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from re import _parser as _re_parser  # type: ignore[attr-defined]
from typing import Any

_NEGATE_CANDIDATES = "abcdefghijkxyz0123456789-_"


def _category_sample(name: str) -> str | None:
    return {
        "CATEGORY_DIGIT": "1",
        "CATEGORY_WORD": "a",
        "CATEGORY_SPACE": " ",
        "CATEGORY_NOT_DIGIT": "a",
        "CATEGORY_NOT_WORD": "-",
        "CATEGORY_NOT_SPACE": "x",
    }.get(name)


def _category_matches(name: str, char: str) -> bool:
    if name == "CATEGORY_DIGIT":
        return char.isdigit()
    if name == "CATEGORY_WORD":
        return char.isalnum() or char == "_"
    if name == "CATEGORY_SPACE":
        return char.isspace()
    if name == "CATEGORY_NOT_DIGIT":
        return not char.isdigit()
    if name == "CATEGORY_NOT_WORD":
        return not (char.isalnum() or char == "_")
    if name == "CATEGORY_NOT_SPACE":
        return not char.isspace()
    return False


def _member_matches(members: list[tuple[Any, Any]], char: str) -> bool:
    for op, arg in members:
        if op.name == "LITERAL" and arg == ord(char):
            return True
        if op.name == "RANGE" and arg[0] <= ord(char) <= arg[1]:
            return True
        if op.name == "CATEGORY" and _category_matches(arg.name, char):
            return True
    return False


def _first_member_char(members: list[tuple[Any, Any]]) -> str | None:
    for op, arg in members:
        if op.name == "LITERAL":
            return chr(arg)
        if op.name == "RANGE":
            return chr(arg[0])
        if op.name == "CATEGORY":
            return _category_sample(arg.name)
    return None


def _emit_in(items: list[tuple[Any, Any]]) -> str | None:
    negate = bool(items) and items[0][0].name == "NEGATE"
    members = items[1:] if negate else items
    if negate:
        for candidate in _NEGATE_CANDIDATES:
            if not _member_matches(members, candidate):
                return candidate
        return None
    return _first_member_char(members)


def _emit_literal(arg: Any) -> str | None:
    return chr(arg)


def _emit_not_literal(arg: Any) -> str | None:
    return "a" if arg != ord("a") else "b"


def _emit_any(arg: Any) -> str | None:
    return "x"


def _emit_repeat(arg: Any) -> str | None:
    minimum, _maximum, subpattern = arg
    sub = _emit_seq(subpattern)
    if sub is None:
        return None
    return sub * (minimum if minimum > 0 else 1)


def _emit_subpattern(arg: Any) -> str | None:
    return _emit_seq(arg[3])


def _emit_branch(arg: Any) -> str | None:
    for branch in arg[1]:
        emitted = _emit_seq(branch)
        if emitted is not None:
            return emitted
    return None


def _emit_at(arg: Any) -> str | None:
    return ""


def _emit_category(arg: Any) -> str | None:
    return _category_sample(arg.name)


def _emit_range(arg: Any) -> str | None:
    return chr(arg[0])


# regex opcode name → the sampler that produces one matching character/run for it.
# MAX_REPEAT and MIN_REPEAT share a sampler; IN delegates to the member-set sampler.
_OPCODE_SAMPLERS: Mapping[str, Callable[[Any], str | None]] = {
    "LITERAL": _emit_literal,
    "NOT_LITERAL": _emit_not_literal,
    "ANY": _emit_any,
    "IN": _emit_in,
    "MAX_REPEAT": _emit_repeat,
    "MIN_REPEAT": _emit_repeat,
    "SUBPATTERN": _emit_subpattern,
    "BRANCH": _emit_branch,
    "AT": _emit_at,
    "CATEGORY": _emit_category,
    "RANGE": _emit_range,
}


def _emit_node(name: str, arg: Any) -> str | None:
    sampler = _OPCODE_SAMPLERS.get(name)
    if sampler is None:
        # Backreferences, look-arounds, and any opcode without a concrete sample are
        # unsupported: the pattern is excluded rather than guessed.
        return None
    return sampler(arg)


def _emit_seq(seq: Any) -> str | None:
    parts: list[str] = []
    for op, arg in seq:
        piece = _emit_node(op.name, arg)
        if piece is None:
            return None
        parts.append(piece)
    return "".join(parts)


def _sample_path_for_pattern(regex: str) -> str | None:
    """A concrete path that PROVABLY matches ``regex`` (validated with ``fullmatch``).

    Returns ``None`` when no representative can be safely derived.
    """
    try:
        parsed = _re_parser.parse(regex)
        compiled = re.compile(regex)
    except re.error:
        return None
    sample = _emit_seq(parsed)
    if sample is None:
        return None
    return sample if compiled.fullmatch(sample) is not None else None
