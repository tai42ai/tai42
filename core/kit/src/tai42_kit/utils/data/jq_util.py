import asyncio
from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache


@lru_cache(maxsize=512)
def get_compiled_jq(expression: str):
    # Opt-in dependency (the "jq" extra) — imported at call time so the module
    # (and the utils.data namespace re-exporting it) stays importable without it.
    import jq

    return jq.compile(expression)


class JqSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="JQ_")

    # Wall-clock budget for one jq evaluation, run on a worker thread. Must be positive.
    timeout_seconds: float = Field(default=10, gt=0)


@settings_cache
def jq_settings() -> JqSettings:
    """Return the process-wide :class:`JqSettings`, cached after first load."""
    return JqSettings()


async def run_jq_first(expression: str, payload: Any) -> Any:
    """Compile (cached) and evaluate ``expression`` over ``payload`` on a worker
    thread, bounded by ``JQ_TIMEOUT_SECONDS``; returns ``.first()``.

    Honest limitation: ``asyncio.to_thread`` cannot kill the C evaluation. On
    timeout the worker thread is abandoned and keeps burning CPU until it
    finishes on its own; the budget only protects the event loop and the
    caller's latency, and the timeout is raised loudly.
    """
    program = get_compiled_jq(expression)
    timeout = jq_settings().timeout_seconds
    try:
        return await asyncio.wait_for(asyncio.to_thread(lambda: program.input(payload).first()), timeout)
    except TimeoutError as exc:
        raise TimeoutError(f"jq evaluation exceeded {timeout}s (JQ_TIMEOUT_SECONDS); expression aborted") from exc
