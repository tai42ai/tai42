"""jq compilation and bounded evaluation in a compute worker, with the process environment sealed out.

The compile and the evaluation of a jq program run in a worker process of
:func:`tai42_kit.compute.compute_pool` (the jq binding holds the GIL for both), under the
``JQ_TIMEOUT_SECONDS`` budget: a job past it has its worker killed, so the caller's event loop
keeps serving while a program runs and a runaway program is stopped. The program shape and the
per-worker compiled cache live in :mod:`~tai42_kit.utils.data.jq_worker`.

The author's program's failure — it does not compile, it raised while it ran, it ran past
``JQ_TIMEOUT_SECONDS``, or it emitted nothing where a value is required — raises a
:class:`~tai42_kit.utils.data.jq_errors.JqProgramError` (``JqCompileError``, ``JqEvaluationError``,
``JqBudgetExceededError``, ``JqNoOutputError``): bad input, never a transport failure. A failure of
the pool to run it raises the pool's own error and is never reworded here. A caller catches
``JqProgramError``, never ``ValueError`` or ``TimeoutError``.
"""

import hashlib
import json
from collections.abc import Iterable
from functools import lru_cache
from typing import Any, Final

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from tai42_kit.compute import compute_pool
from tai42_kit.settings import TaiBaseSettings, settings_cache
from tai42_kit.utils.data.jq_errors import JqBudgetExceededError, JqNoOutputError
from tai42_kit.utils.data.jq_worker import (
    JqSource,
    compile_and_evaluate_job,
    compile_job,
    envelope,
    evaluate_cached_job,
)

__all__ = ["JqSettings", "compile_check", "jq_settings", "run_jq_bounded", "run_jq_first"]

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


def _envelope_text(document: Any) -> str:
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
        return orjson.dumps(document, default=_to_json_base, option=option).decode()
    except orjson.JSONEncodeError as exc:
        if isinstance(exc.__cause__, TypeError):
            raise exc.__cause__ from None
        if exc.__cause__ is None and str(exc) in _JSON_DUMPS_FALLBACK:
            return json.dumps(document)
        raise


@lru_cache(maxsize=512)
def _program_key(expression: str, prelude: str, variables: tuple[str, ...]) -> str:
    """The digest a worker caches the program built from these three under.

    sha256 over each part's length and bytes, so no two sources share a digest by
    concatenation. Memoized here so a warm caller re-hashes no program text. Any ``str``
    hashes, a lone surrogate included: whether the text is valid is the compile's to say.
    """
    digest = hashlib.sha256()
    for part in (expression, prelude, *variables):
        data = part.encode("utf-8", "surrogatepass")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


class JqSettings(TaiBaseSettings):
    """Process-wide jq settings, read from ``JQ_``-prefixed environment variables."""

    model_config = SettingsConfigDict(env_prefix="JQ_")

    # Budget in seconds for one jq job (a compile, an evaluation, or both) in its
    # compute worker, counted from the moment a worker takes the job up; past it the worker
    # is killed and the caller gets ``JqBudgetExceededError``. Must be positive.
    timeout_seconds: float = Field(default=10, gt=0)


@settings_cache
def jq_settings() -> JqSettings:
    """Return the process-wide :class:`JqSettings`, cached after first load."""
    return JqSettings()


async def compile_check(expression: str, *, prelude: str = "", variables: Iterable[str] = ()) -> None:
    """Prove ``expression`` is a valid jq program, with ``variables`` predeclared as named ``$name`` bindings.

    jq resolves variable references at compile time, so an expression that will read a
    ``$name`` bound only at evaluation must have that name declared here or it fails to
    compile; the bound VALUES are irrelevant to a compile check. ``prelude`` is the run of
    ``def`` declarations the expression may call, as in :func:`run_jq_first`. Raises
    :class:`~tai42_kit.utils.data.jq_errors.JqCompileError` on a syntax error, a reference to a
    variable outside ``variables``, or a reserved name. The compile runs in a compute worker under
    ``JQ_TIMEOUT_SECONDS`` (:class:`~tai42_kit.utils.data.jq_errors.JqBudgetExceededError` past it)
    and leaves the program in that worker's cache.
    """
    names = tuple(sorted(set(variables)))
    key = _program_key(expression, prelude, names)
    timeout = jq_settings().timeout_seconds
    try:
        await compute_pool().run(compile_job, key, JqSource(expression, prelude, names), timeout=timeout)
    except TimeoutError as exc:
        # The pool's timeout of a jq job is the author's program running past its budget.
        raise JqBudgetExceededError(
            f"jq compile exceeded {timeout}s (JQ_TIMEOUT_SECONDS); its worker was killed"
        ) from exc


async def _evaluate(expression: str, payload: Any, prelude: str, variables: dict[str, Any], count: int) -> list[Any]:
    """The first ``count`` values ``expression`` emits over ``payload``, evaluated in a compute worker.

    The worker is sent the program's digest and the envelope text; only when it holds no
    program under that digest is the source sent, compiled there and cached.
    """
    names = tuple(sorted(variables))
    key = _program_key(expression, prelude, names)
    input_text = _envelope_text(envelope(payload, variables))
    timeout = jq_settings().timeout_seconds
    pool = compute_pool()
    try:
        values = await pool.run(evaluate_cached_job, key, input_text, count, timeout=timeout)
        if values is None:
            source = JqSource(expression, prelude, names)
            values = await pool.run(compile_and_evaluate_job, key, source, input_text, count, timeout=timeout)
    except TimeoutError as exc:
        raise JqBudgetExceededError(
            f"jq evaluation exceeded {timeout}s (JQ_TIMEOUT_SECONDS); its worker was killed"
        ) from exc
    return values


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
    """Compile (cached per worker) and evaluate ``expression`` over ``payload`` in a compute worker.

    Bounded by ``JQ_TIMEOUT_SECONDS`` (the worker is killed past it and
    :class:`~tai42_kit.utils.data.jq_errors.JqBudgetExceededError` raises); returns the first
    emitted value.

    ``variables`` predeclares named jq variables: each key ``k`` is readable as ``$k`` in
    the expression, bound to its value delivered per call through the evaluation envelope.
    An expression referencing an undeclared ``$name`` fails loudly at compile
    (:class:`~tai42_kit.utils.data.jq_errors.JqCompileError`, jq: not defined), so a caller that omits a variable the
    expression needs never silently degrades. The compile is cached on the variable NAMES,
    so repeated calls with the same names and different VALUES reuse the one compiled
    program.

    On an empty pipeline: returns ``default`` when one was supplied, else raises
    :class:`~tai42_kit.utils.data.jq_errors.JqNoOutputError` — never silently ``None`` (an
    empty pipeline is distinct from a real ``None`` result).
    """
    values = await _evaluate(expression, payload, prelude, variables or {}, 1)
    if values:
        return values[0]
    if default is _NO_DEFAULT:
        raise JqNoOutputError(f"jq expression produced no output (empty pipeline): {expression!r}")
    return default


async def run_jq_bounded(
    expression: str,
    payload: Any,
    limit: int,
    *,
    prelude: str = "",
    variables: dict[str, Any] | None = None,
) -> list[Any]:
    """Compile (cached per worker) and evaluate ``expression`` over ``payload`` in a compute worker.

    Bounded by ``JQ_TIMEOUT_SECONDS``; returns AT MOST ``limit + 1`` emitted values, taken lazily
    from the program's iterator. ``variables`` behaves exactly as in :func:`run_jq_first`.

    For a caller that must enforce an exact emit count: it passes its allowed count as
    ``limit`` and reads ``len(result) > limit`` as "emitted too many". The extra slot
    lets an over-emit be distinguished from an exact ``limit`` without ever
    materializing the full stream. The bound is on the NUMBER of values taken (at most
    ``limit + 1``); a single value's size is bounded only by the timeout, not by
    ``limit``. ``limit`` must be positive (a caller's misuse raises a plain ``ValueError``). Same
    budget semantics as :func:`run_jq_first`.
    """
    if limit < 1:
        raise ValueError(f"run_jq_bounded limit must be positive, got {limit}")
    return await _evaluate(expression, payload, prelude, variables or {}, limit + 1)
