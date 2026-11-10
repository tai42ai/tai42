"""The jq seam's program shape, its compile, and the jobs a compute worker runs.

Both sides of the seam use the program shape: the evaluation input every compiled program reads
(:func:`envelope`) and the source a program is built from (:class:`JqSource`). The rest runs inside
a worker process of
:func:`tai42_kit.compute.compute_pool`: a program is built from a :class:`JqSource` with the process
environment sealed out, compiled (:func:`compile_jq`), and cached in the worker under the digest the
caller computed from that source; the caller sends the digest with every evaluation and the source
only when the worker reports a miss, so a warm evaluation ships the digest and the input text alone.
A program that does not compile raises :class:`~tai42_kit.utils.data.jq_errors.JqCompileError`; one
that raises while it runs, :class:`~tai42_kit.utils.data.jq_errors.JqEvaluationError`.
"""

import itertools
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from tai42_kit.utils.data.jq_errors import JqCompileError, JqEvaluationError

# Guard prefix that seals the process environment out of every compiled
# expression: ``env`` is shadowed by a builtin that raises when called (it cannot
# be substring-scanned — ``.env`` and ``{env: …}`` are legitimate), and ``$ENV``
# is bound to an empty object as the defense-in-depth floor. ``$ENV`` itself is a
# literal 4-char token with no splicing, so the compile rejects it up front with
# zero false negatives. The substring reject over-rejects (false positive) an
# expression that merely contains ``$ENV`` inside a string literal, key, or
# comment — a loud refusal, the correct bias for a security gate.
# The prefix opens no paren: in jq's grammar both ``def …; Exp`` and
# ``Term as $x | Exp`` take the whole remaining program, so the guarded program
# compiles exactly when the bare one does and an unbalanced ``)`` in the
# expression cannot close a scope the guard opened.
_GUARD_PREAMBLE = (
    'def env: error("jq: the env builtin is disabled '
    '(process environment is not readable from expressions)"); '
    "{} as $ENV | "
)

# The two envelope keys every compiled program reads its evaluation input from:
# ``d`` carries the data the expression is about (its ``.``), ``v`` the map of
# variable values. The binding preamble unpacks both.
_ENVELOPE_DATA = "d"
_ENVELOPE_VARS = "v"

# Variable names an author may not bind, because the binding mechanism and jq
# reserve them: ``__in`` is the envelope binding, ``ENV`` the sealed environment
# object, ``__loc__`` a jq built-in location variable. ``__in`` is not readable
# either: an expression or a prelude that names ``$__in`` is refused before the
# compile (``_ENVELOPE_VARIABLE_RE``), so the data reaches a program only as ``.``
# and each variable only as its own ``$name``.
_RESERVED_VARIABLE_NAMES = frozenset({"__in", "ENV", "__loc__"})

# ``$__in`` as a whole token (``$__inbox`` is another name). Like the ``$ENV``
# check, a match inside a string literal or a comment is refused too.
_ENVELOPE_VARIABLE_RE = re.compile(r"\$__in\b")

# A name jq can both read as a field (``$__in.v.<name>``) and bind as ``$<name>``.
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# The header line jq gives each compile error, ``<top-level>`` being the compiled text. Anchored to
# the whole line, so a location-shaped string in the author's text, which jq quotes only on the
# indented snippet line below the header, is never matched; the greedy head takes the last location.
_ERROR_HEADER_RE = re.compile(r"^(jq: error: .*) at <top-level>, line (\d+), column (\d+):$", re.MULTILINE)


# How many compiled programs one worker keeps; the least recently used is dropped first.
COMPILED_CACHE_SIZE = 512


def envelope(payload: Any, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """The evaluation input every compiled program takes.

    The data lands under ``d`` (the expression's ``.``) and the variable values under
    ``v``; the binding preamble the compile generates unpacks both back into ``.`` and
    the named ``$name`` bindings.
    """
    return {_ENVELOPE_VARS: dict(variables or {}), _ENVELOPE_DATA: payload}


@dataclass(frozen=True, slots=True)
class JqSource:
    """What a program is built from.

    ``prelude`` is a run of ``def`` declarations the expression may call; ``variables`` the
    tuple of variable NAMES the expression may read as ``$name`` (their values arrive per
    evaluation through the envelope).
    """

    expression: str
    prelude: str
    variables: tuple[str, ...]


def _binding_preamble(variable_names: tuple[str, ...]) -> str:
    """The generated jq that unpacks the ``{"v": {…}, "d": data}`` envelope.

    ``.`` starts as the envelope: it is captured as ``$__in``, each name is bound from
    ``$__in.v.<name>`` to ``$<name>`` (readable anywhere, inside ``map(...)`` too), and
    ``$__in.d`` restores the data as ``.``. The author's text (prelude, then expression)
    follows the closing pipe whole, so the program compiles exactly when the author's text
    does with the variable names defined. The preamble holds no newline.
    """
    bindings = "".join(f"$__in.{_ENVELOPE_VARS}.{name} as ${name} | " for name in variable_names)
    return f". as $__in | {bindings}$__in.{_ENVELOPE_DATA} | "


def _author_message(message: str, prelude_lines: int) -> str:
    """Locate jq's compile error ``message`` for the compiled text in the author's own lines.

    The compiled text is one generated line followed by the author's text, so jq's snippet and
    caret already show the author's line and only the line number of each header moves: line
    ``L`` of the compiled text is the author's line ``L - 1``, ``at <prelude>`` on one of the
    prelude's ``prelude_lines`` lines, else ``at <top-level>`` on the expression's own line.
    """

    def _locate(match: re.Match[str]) -> str:
        head, line, column = match.group(1), int(match.group(2)) - 1, match.group(3)
        if line <= prelude_lines:
            return f"{head} at <prelude>, line {line}, column {column}:"
        return f"{head} at <top-level>, line {line - prelude_lines}, column {column}:"

    return _ERROR_HEADER_RE.sub(_locate, message)


def compile_jq(source: JqSource):
    """Compile ``source`` over the envelope with the process environment sealed out.

    Raises :class:`JqCompileError` on a syntax error (a ``module``, ``include`` or ``import``
    directive among them), a reference to an undeclared ``$name``, a reserved variable name, a
    variable name that is not a jq identifier, a reference to ``$ENV`` / ``$__in``, a NUL
    character, text that is not valid Unicode, or an empty expression; jq's message names the
    author's own line and column, ``at <prelude>`` for an error inside the prelude. Inside the
    program ``$__loc__`` reports the compiled text's line, the author's line plus one.
    """
    # Opt-in dependency (the "jq" extra) — imported at call time so the module
    # (and the utils.data namespace) stays importable without it.
    import jq

    expression, prelude, variable_names = source.expression, source.prelude, source.variables
    if "$ENV" in expression or "$ENV" in prelude:
        raise JqCompileError("jq: $ENV is disabled (process environment is not readable from expressions)")
    if _ENVELOPE_VARIABLE_RE.search(expression) or _ENVELOPE_VARIABLE_RE.search(prelude):
        raise JqCompileError("jq: $__in is reserved (the binding envelope is not readable from expressions)")
    reserved = sorted(_RESERVED_VARIABLE_NAMES.intersection(variable_names))
    if reserved:
        raise JqCompileError(f"jq: the variable name(s) {reserved} are reserved and cannot be bound as jq variables")
    invalid = sorted(name for name in variable_names if not _VARIABLE_NAME_RE.fullmatch(name))
    if invalid:
        raise JqCompileError(f"jq: the variable name(s) {invalid} are not jq identifiers and cannot be bound")
    # jq takes the program as a NUL-terminated C string, so it would compile the text before a NUL.
    if "\x00" in expression or "\x00" in prelude:
        raise JqCompileError("jq: the program contains a NUL character (U+0000), which jq cannot compile")
    # The prelude ends with a newline so the expression starts its own line, the one
    # after the prelude's last.
    if prelude and not prelude.endswith("\n"):
        prelude += "\n"
    author_text = prelude + expression
    try:
        author_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise JqCompileError(f"jq: the program is not valid Unicode text at offset {exc.start}: {exc.reason}") from exc
    if not expression.strip():
        raise JqCompileError('jq: the expression is empty (try ".")')
    # The generated text is exactly one line, so the author's text starts on line 2 whole.
    generated = _GUARD_PREAMBLE + _binding_preamble(variable_names) + "\n"
    try:
        return jq.compile(generated + author_text)
    except ValueError as exc:
        raise JqCompileError(_author_message(str(exc), prelude.count("\n"))) from exc


# This worker's compiled programs, keyed by the caller's digest of their source, oldest first.
_compiled: OrderedDict[str, Any] = OrderedDict()


def _cached(key: str) -> Any | None:
    program = _compiled.get(key)
    if program is not None:
        _compiled.move_to_end(key)
    return program


def _compile_into_cache(key: str, source: JqSource) -> Any:
    program = compile_jq(source)
    _compiled[key] = program
    if len(_compiled) > COMPILED_CACHE_SIZE:
        _compiled.popitem(last=False)
    return program


def _take(program: Any, input_text: str, count: int) -> list[Any]:
    # Nothing but the program runs inside the iteration, so jq's ``ValueError`` here is the program's.
    try:
        return list(itertools.islice(program.input_text(input_text), count))
    except ValueError as exc:
        raise JqEvaluationError(str(exc)) from exc


def compile_job(key: str, source: JqSource) -> None:
    """Compile ``source`` into this worker's cache under ``key``; raises the compile's :class:`JqCompileError`."""
    if _cached(key) is None:
        _compile_into_cache(key, source)


def evaluate_cached_job(key: str, input_text: str, count: int) -> list[Any] | None:
    """The first ``count`` values the program cached under ``key`` emits over ``input_text``.

    ``None`` when this worker holds no program under ``key``: the caller then sends the
    source through :func:`compile_and_evaluate_job`. A program that raises while it runs
    raises :class:`JqEvaluationError` with jq's message.
    """
    program = _cached(key)
    if program is None:
        return None
    return _take(program, input_text, count)


def compile_and_evaluate_job(key: str, source: JqSource, input_text: str, count: int) -> list[Any]:
    """Compile ``source`` into this worker's cache under ``key``, then evaluate as :func:`evaluate_cached_job`."""
    program = _cached(key)
    if program is None:
        program = _compile_into_cache(key, source)
    return _take(program, input_text, count)
