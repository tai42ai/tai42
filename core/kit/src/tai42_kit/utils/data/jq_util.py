import asyncio
import itertools
import re
from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache

# Guard wrapper that seals the process environment out of every compiled
# expression: ``env`` is shadowed by a builtin that raises when called (it cannot
# be substring-scanned — ``.env`` and ``{env: …}`` are legitimate), and ``$ENV``
# is bound to an empty object as the defense-in-depth floor. ``$ENV`` itself is a
# literal 4-char token with no splicing, so the caller rejects it up front with
# zero false negatives. The substring reject over-rejects (false positive) an
# expression that merely contains ``$ENV`` inside a string literal, key, or
# comment — a loud refusal, the correct bias for a security gate.
_GUARD_PREAMBLE = (
    'def env: error("jq: the env builtin is disabled '
    '(process environment is not readable from expressions)"); '
    "{} as $ENV | ("
)


@lru_cache(maxsize=512)
def get_compiled_jq(expression: str, prelude: str = ""):
    # Opt-in dependency (the "jq" extra) — imported at call time so the module
    # (and the utils.data namespace re-exporting it) stays importable without it.
    import jq

    if "$ENV" in expression or "$ENV" in prelude:
        raise ValueError("jq: $ENV is disabled (process environment is not readable from expressions)")
    # ``prelude`` is a run of ``def …;`` declarations the expression may call. It
    # ends with a newline so the expression's first line is line ``prelude_lines
    # + 1`` — keeping the raw-compile error's line arithmetic exact below.
    if prelude and not prelude.endswith("\n"):
        prelude += "\n"
    prelude_lines = prelude.count("\n")
    # Raw compile first so a syntax error reports the author's own line/column.
    # A bare ``expression`` calling a prelude def does not compile alone, so the
    # raw compile runs over ``prelude + expression``; a prelude shifts the line
    # numbers, so on error re-raise with the prelude's line count subtracted.
    try:
        jq.compile(prelude + expression)
    except ValueError as exc:
        if not prelude:
            raise
        message = str(exc)

        def _shift(match: re.Match[str]) -> str:
            return f"{match.group(1)}{int(match.group(2)) - prelude_lines}"

        raise ValueError(re.sub(r"(, line )(\d+)", _shift, message)) from exc
    # The ``\n)`` closes the preamble paren past any trailing line comment.
    return jq.compile(_GUARD_PREAMBLE + prelude + expression + "\n)")


class JqSettings(TaiBaseSettings):
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


async def run_jq_first(expression: str, payload: Any, *, default: Any = _NO_DEFAULT, prelude: str = "") -> Any:
    """Compile (cached) and evaluate ``expression`` over ``payload`` on a worker
    thread, bounded by ``JQ_TIMEOUT_SECONDS``; returns ``.first()``.

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
    program = get_compiled_jq(expression, prelude)
    timeout = jq_settings().timeout_seconds

    def _run() -> Any:
        try:
            return program.input(payload).first()
        except StopIteration:
            if default is _NO_DEFAULT:
                raise ValueError(f"jq expression produced no output (empty pipeline): {expression!r}") from None
            return default

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout)
    except TimeoutError as exc:
        raise TimeoutError(f"jq evaluation exceeded {timeout}s (JQ_TIMEOUT_SECONDS); expression aborted") from exc


async def run_jq_bounded(expression: str, payload: Any, limit: int, *, prelude: str = "") -> list[Any]:
    """Compile (cached) and evaluate ``expression`` over ``payload`` on a worker
    thread, bounded by ``JQ_TIMEOUT_SECONDS``; returns AT MOST ``limit + 1`` emitted
    values, taken lazily from the program's iterator.

    For a caller that must enforce an exact emit count: it passes its allowed count as
    ``limit`` and reads ``len(result) > limit`` as "emitted too many". The extra slot
    lets an over-emit be distinguished from an exact ``limit`` without ever
    materializing the full stream. The bound is on the NUMBER of values taken (at most
    ``limit + 1``); a single value's size is bounded only by the timeout, not by
    ``limit``. ``limit`` must be positive. Same timeout semantics as :func:`run_jq_first`.
    """
    if limit < 1:
        raise ValueError(f"run_jq_bounded limit must be positive, got {limit}")
    program = get_compiled_jq(expression, prelude)
    timeout = jq_settings().timeout_seconds
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(lambda: list(itertools.islice(program.input(payload), limit + 1))), timeout
        )
    except TimeoutError as exc:
        raise TimeoutError(f"jq evaluation exceeded {timeout}s (JQ_TIMEOUT_SECONDS); expression aborted") from exc
