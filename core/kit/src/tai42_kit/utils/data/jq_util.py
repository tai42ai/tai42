"""jq expression compilation and bounded evaluation, with the process environment sealed out."""

import asyncio
import itertools
import json
import re
from collections.abc import Iterable
from functools import lru_cache
from typing import Any, Final

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache

# Guard prefix that seals the process environment out of every compiled
# expression: ``env`` is shadowed by a builtin that raises when called (it cannot
# be substring-scanned — ``.env`` and ``{env: …}`` are legitimate), and ``$ENV``
# is bound to an empty object as the defense-in-depth floor. ``$ENV`` itself is a
# literal 4-char token with no splicing, so the caller rejects it up front with
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


def _envelope(payload: Any, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """The evaluation input every compiled program takes.

    The data lands under ``d`` (the expression's ``.``) and the variable values under
    ``v``; the binding preamble the compile generates unpacks both back into ``.`` and
    the named ``$name`` bindings.
    """
    return {_ENVELOPE_VARS: dict(variables or {}), _ENVELOPE_DATA: payload}


# orjson's own refusals (no ``__cause__``) of input ``json.dumps`` encodes or
# refuses by its own rules: an int above 64 bits, a dict key that is not an exact
# ``str``, nesting past orjson's recursion limit. The envelope then goes to
# ``json.dumps`` whole, so jq gets the same text (or the caller the same error).
_JSON_DUMPS_FALLBACK: Final = frozenset(
    {"Integer exceeds 64-bit range", "Dict key must be str", "Recursion limit reached"}
)


def _to_json_base(o: Any) -> Any:
    """Map what the passthrough options route here to the JSON value ``json.dumps`` gives it."""
    if isinstance(o, dict):
        return dict(o)
    if isinstance(o, (list, tuple)):
        return list(o)
    if isinstance(o, str):
        return str(o)
    if isinstance(o, int):
        return int(o)
    if isinstance(o, float):
        return float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _envelope_text(envelope: Any) -> str:
    """The envelope as JSON text for jq's ``input_text``.

    Every value and key gives the jq result ``json.dumps`` text gives, or the same
    ``TypeError`` text, except in an all-``str``-key envelope: a non-finite float
    reaches jq as ``null``, and a plain ``Enum`` member or a ``UUID`` encodes as its
    value or string where ``json.dumps`` refuses it.
    """
    import orjson

    # No ``OPT_NON_STR_KEYS``: its key texts differ from ``json.dumps``', so any key
    # that is not an exact ``str`` takes the fallback below. The passthrough options
    # route datetimes, dataclasses and every container/scalar subclass to
    # ``_to_json_base``.
    option = orjson.OPT_PASSTHROUGH_DATETIME | orjson.OPT_PASSTHROUGH_DATACLASS | orjson.OPT_PASSTHROUGH_SUBCLASS
    try:
        return orjson.dumps(envelope, default=_to_json_base, option=option).decode()
    except orjson.JSONEncodeError as exc:
        if isinstance(exc.__cause__, TypeError):
            raise exc.__cause__ from None
        if exc.__cause__ is None and str(exc) in _JSON_DUMPS_FALLBACK:
            return json.dumps(envelope)
        raise


def _binding_preamble(prelude: str, variable_names: tuple[str, ...]) -> str:
    """The generated jq that unpacks the ``{"v": {…}, "d": data}`` envelope.

    ``.`` starts as the envelope: it is captured as ``$__in``, each name is bound from
    ``$__in.v.<name>`` to ``$<name>`` (readable anywhere, inside ``map(...)`` too), the
    ``prelude`` ``def``s follow the bindings so they may read the variables, and
    ``$__in.d`` restores the data as ``.`` for the expression. The prelude is a run of
    ``def …;`` and takes NO pipe after it.
    """
    bindings = "".join(f"$__in.{_ENVELOPE_VARS}.{name} as ${name} | " for name in variable_names)
    return f". as $__in | {bindings}{prelude}$__in.{_ENVELOPE_DATA} | "


def _compile_jq(expression: str, prelude: str, variable_names: tuple[str, ...]):
    # Opt-in dependency (the "jq" extra) — imported at call time so the module
    # (and the utils.data namespace re-exporting it) stays importable without it.
    import jq

    if "$ENV" in expression or "$ENV" in prelude:
        raise ValueError("jq: $ENV is disabled (process environment is not readable from expressions)")
    if _ENVELOPE_VARIABLE_RE.search(expression) or _ENVELOPE_VARIABLE_RE.search(prelude):
        raise ValueError("jq: $__in is reserved (the binding envelope is not readable from expressions)")
    reserved = sorted(_RESERVED_VARIABLE_NAMES.intersection(variable_names))
    if reserved:
        raise ValueError(f"jq: the variable name(s) {reserved} are reserved and cannot be bound as jq variables")
    # ``prelude`` is a run of ``def …;`` declarations the expression may call. It
    # ends with a newline so the expression's first line is line ``prelude_lines
    # + 1`` — keeping the bare-compile error's line arithmetic exact below. The
    # binding preamble adds no newline, so it does not shift the expression's line.
    if prelude and not prelude.endswith("\n"):
        prelude += "\n"
    prelude_lines = prelude.count("\n")
    binding = _binding_preamble(prelude, variable_names)
    try:
        return jq.compile(_GUARD_PREAMBLE + binding + expression)
    except ValueError:
        # The guarded message carries the guard text and shifted columns, so the
        # bare program is compiled to word the failure in the author's own
        # line/column, with the prelude's line count subtracted.
        try:
            jq.compile(binding + expression)
        except ValueError as exc:
            if not prelude:
                raise
            message = str(exc)

            def _shift(match: re.Match[str]) -> str:
                return f"{match.group(1)}{int(match.group(2)) - prelude_lines}"

            raise ValueError(re.sub(r"(, line )(\d+)", _shift, message)) from exc
        raise


@lru_cache(maxsize=512)
def get_compiled_jq(expression: str, prelude: str = "", variables: tuple[str, ...] = ()):
    """Compile ``expression`` (LRU-cached) over the ``{"v": …, "d": data}`` envelope.

    ``prelude`` is an optional run of ``def`` declarations the expression may call.
    ``variables`` is the tuple of variable NAMES the expression may read as ``$name``;
    the compile is keyed on ``(expression, prelude, variables)`` and the VALUES are
    delivered per evaluation through the envelope, so one compiled program serves every
    set of values for the same names. A caller that assembles ``variables`` from an
    unordered set passes it sorted so the cache keys on a stable name tuple.
    """
    return _compile_jq(expression, prelude, variables)


def compile_check(expression: str, *, variables: Iterable[str] = ()) -> None:
    """Prove ``expression`` is a valid jq program, with ``variables`` predeclared as named ``$name`` bindings.

    jq resolves variable references at compile time, so
    an expression that will read a ``$name`` bound only at evaluation must have that name
    declared here or it fails to compile; the bound VALUES are irrelevant to a compile
    check. Raises ``ValueError`` on a syntax error or a reference to a
    variable outside ``variables``. Compiles through the shared (cached) path — a caller
    wanting to run it later reuses the same compiled program via :func:`run_jq_first`.
    """
    get_compiled_jq(expression, "", tuple(sorted(set(variables))))


class JqSettings(TaiBaseSettings):
    """Process-wide jq settings, read from ``JQ_``-prefixed environment variables."""

    model_config = SettingsConfigDict(env_prefix="JQ_")

    # Wall-clock budget for one jq evaluation, run on a worker thread. Must be positive.
    timeout_seconds: float = Field(default=10, gt=0)


@settings_cache
def jq_settings() -> JqSettings:
    """Return the process-wide :class:`JqSettings`, cached after first load."""
    return JqSettings()


# Sentinel distinguishing "no default supplied" from a caller passing ``None`` as
# the default (``None`` is a legitimate empty-pipeline substitute).
_NO_DEFAULT = object()


async def run_jq_first(
    expression: str,
    payload: Any,
    *,
    default: Any = _NO_DEFAULT,
    prelude: str = "",
    variables: dict[str, Any] | None = None,
) -> Any:
    """Compile (cached) and evaluate ``expression`` over ``payload`` on a worker thread.

    Bounded by ``JQ_TIMEOUT_SECONDS``; returns ``.first()``.

    ``variables`` predeclares named jq variables: each key ``k`` is readable as ``$k`` in
    the expression, bound to its value delivered per call through the evaluation envelope.
    An expression referencing an undeclared ``$name`` fails loudly at compile (jq: not
    defined), so a caller that omits a variable the expression needs never silently
    degrades. The compile is cached on the variable NAMES, so repeated calls with the same
    names and different VALUES reuse the one compiled program.

    On an empty pipeline (``.first()`` raises ``StopIteration``, which cannot cross
    the ``to_thread`` future boundary so it is converted in the worker thread):
    returns ``default`` when one was supplied, else raises ``ValueError`` — never
    the opaque ``RuntimeError``, and never silently ``None`` (an empty pipeline is
    distinct from a real ``None`` result).

    Honest limitation: ``asyncio.to_thread`` cannot kill the C evaluation. On
    timeout the worker thread is abandoned and keeps burning CPU until it
    finishes on its own; the budget only protects the event loop and the
    caller's latency, and the timeout is raised loudly.
    """
    values = variables or {}
    program = get_compiled_jq(expression, prelude, tuple(sorted(values)))
    envelope = _envelope(payload, values)
    timeout = jq_settings().timeout_seconds

    def _run() -> Any:
        try:
            return program.input_text(_envelope_text(envelope)).first()
        except StopIteration:
            if default is _NO_DEFAULT:
                raise ValueError(f"jq expression produced no output (empty pipeline): {expression!r}") from None
            return default

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout)
    except TimeoutError as exc:
        raise TimeoutError(f"jq evaluation exceeded {timeout}s (JQ_TIMEOUT_SECONDS); expression aborted") from exc


async def run_jq_bounded(
    expression: str,
    payload: Any,
    limit: int,
    *,
    prelude: str = "",
    variables: dict[str, Any] | None = None,
) -> list[Any]:
    """Compile (cached) and evaluate ``expression`` over ``payload`` on a worker thread.

    Bounded by ``JQ_TIMEOUT_SECONDS``; returns AT MOST ``limit + 1`` emitted values, taken lazily
    from the program's iterator. ``variables`` behaves exactly as in :func:`run_jq_first`.

    For a caller that must enforce an exact emit count: it passes its allowed count as
    ``limit`` and reads ``len(result) > limit`` as "emitted too many". The extra slot
    lets an over-emit be distinguished from an exact ``limit`` without ever
    materializing the full stream. The bound is on the NUMBER of values taken (at most
    ``limit + 1``); a single value's size is bounded only by the timeout, not by
    ``limit``. ``limit`` must be positive. Same timeout semantics as :func:`run_jq_first`.
    """
    if limit < 1:
        raise ValueError(f"run_jq_bounded limit must be positive, got {limit}")
    values = variables or {}
    program = get_compiled_jq(expression, prelude, tuple(sorted(values)))
    envelope = _envelope(payload, values)
    timeout = jq_settings().timeout_seconds
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(lambda: list(itertools.islice(program.input_text(_envelope_text(envelope)), limit + 1))),
            timeout,
        )
    except TimeoutError as exc:
        raise TimeoutError(f"jq evaluation exceeded {timeout}s (JQ_TIMEOUT_SECONDS); expression aborted") from exc
