"""Batch execution for the ``batch`` tool extension.

Runs one instance of a named tool per input parameter set, sequentially or in
parallel, returning results in input order. The composed signature the extension
presents lives with its factory; this module holds only the runner.
"""

import asyncio
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_kit.settings import TaiBaseSettings, settings_cache


class BatchSettings(TaiBaseSettings):
    """Concurrency and size bounds for the ``batch`` tool extension (env prefix
    ``BATCH_``)."""

    model_config = SettingsConfigDict(env_prefix="BATCH_")

    # Concurrency for a parallel call with no max_concurrent, floored to input size.
    default_max_concurrent: int = Field(default=5, gt=0)
    # Hard ceiling on len(params) per call; an over-limit call raises loudly.
    max_batch_size: int = Field(default=100, gt=0)


@settings_cache
def batch_settings() -> BatchSettings:
    return BatchSettings()


async def execute_batch(
    tool_name: str,
    params: list[dict[str, Any]],
    execution_mode: Literal["sequential", "parallel"] = "sequential",
    max_concurrent: int | None = None,
    fail_fast: bool = True,
) -> list[Any]:
    """Run ``tool_name`` once per param set, returning results in input order.

    With ``fail_fast`` (the default) the first failing item raises loudly and the
    call aborts; with ``fail_fast=False`` a failing item's error string takes its
    slot so the result list stays the same length and order as the input.
    """
    max_batch_size = batch_settings().max_batch_size
    if len(params) > max_batch_size:
        raise ValueError(f"batch got {len(params)} param sets; the limit is {max_batch_size} (BATCH_MAX_BATCH_SIZE)")

    async def process_single(param: dict[str, Any], sem: asyncio.Semaphore | None = None) -> Any:
        try:
            if sem:
                async with sem:
                    return await tai42_app.tools.run_tool(tool_name, param)
            return await tai42_app.tools.run_tool(tool_name, param)
        except Exception as exc:
            if fail_fast:
                raise
            return str(exc)

    if execution_mode == "parallel":
        if max_concurrent is not None and max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive integer")
        # Unset cap defaults to the setting, floored to input size (min 1 so an empty batch is valid).
        concurrent_limit = (
            max_concurrent
            if max_concurrent is not None
            else min(batch_settings().default_max_concurrent, len(params) or 1)
        )
        sem = asyncio.Semaphore(concurrent_limit)
        # First failure propagates; cancel in-flight siblings and drain before re-raising.
        tasks = [asyncio.ensure_future(process_single(param, sem)) for param in params]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    if execution_mode == "sequential":
        return [await process_single(param) for param in params]
    raise RuntimeError(f"Unknown execution mode '{execution_mode}'")
