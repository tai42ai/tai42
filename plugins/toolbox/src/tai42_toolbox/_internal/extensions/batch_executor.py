"""Batch execution for the ``batch`` tool extension.

Runs one instance of a named tool per input parameter set, sequentially or in
parallel, returning results in input order. The composed signature the extension
presents lives with its factory; this module holds only the runner.
"""

import asyncio
from typing import Any, Literal, cast

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_contract.interactions import SuspendedInteraction
from tai42_kit.settings import TaiBaseSettings, settings_cache


class BatchMultiParkUnsupported(Exception):
    """Raised when more than one body in a single ``batch`` call async-parked.

    Multi-park reassembly is unsupported at the batch layer BY DESIGN: a batch is ONE tool
    call and can surface only ONE park signal, so it cannot coordinate the N barriers / N
    completions that fanning parking work out would require. Two-plus parks would corrupt
    silently — each parked body captured the SAME bound completion and would fire it under a
    DIFFERENT ``completion_id`` on resume, so a delivery cannot dedup them. Reassembly is out
    of scope.

    This is a LOUD guard, NOT a rollback: by the time a body's ``run_tool`` returned a
    sentinel its park was ALREADY durably persisted, and raising here does NOT unwind those —
    so N parks may be left orphaned. ``batch`` is single-park only; work that must fan out
    multiple concurrent parks needs a purpose-built multi-park coordinator, not ``batch``."""

    def __init__(self, tool_name: str, interaction_ids: list[str]) -> None:
        self.tool_name = tool_name
        self.interaction_ids = interaction_ids
        super().__init__(
            f"batch of '{tool_name}' had {len(interaction_ids)} bodies async-park in one call "
            f"(interactions {interaction_ids}); multi-park is unsupported at the batch layer. Those "
            f"parks are ALREADY persisted and are NOT unwound by this error, so they may be left "
            f"orphaned. batch is single-park only; multi-park fan-out needs a purpose-built coordinator."
        )


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
) -> list[Any] | SuspendedInteraction:
    """Run ``tool_name`` once per param set, returning results in input order.

    With ``fail_fast`` (the default) the first failing item raises loudly and the
    call aborts; with ``fail_fast=False`` a failing item's error string takes its
    slot so the result list stays the same length and order as the input.

    If a body async-parks it returns the ``SuspendedInteraction`` park SIGNAL. A batch is ONE
    tool call and surfaces ONE park: exactly one parked body PROPAGATES its sentinel (the
    batch parks as a whole, so the caller's park recognition fires rather than the batch
    reporting a partial result list over a hidden pause). TWO-plus parks are unsupported at
    the batch layer and raise :class:`BatchMultiParkUnsupported` loudly, naming the parked
    interactions — the guard keys on the SENTINEL count only, so errored bodies (error
    strings under ``fail_fast=False``) never trip it. The guard does not unwind the
    already-persisted parks.
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
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    elif execution_mode == "sequential":
        results = [await process_single(param) for param in params]
    else:
        raise RuntimeError(f"Unknown execution mode '{execution_mode}'")

    # If a body parked, PROPAGATE the park: the batch parks as a whole, re-surfacing the one
    # sentinel so the caller's park recognition fires rather than the batch reporting a partial
    # result list over a hidden pause. TWO-plus parks are unsupported at the batch layer (one
    # tool call surfaces one signal) and are GUARDED loudly — the guard keys on the SENTINEL
    # count only, so errored bodies (error strings, not sentinels, under fail_fast=False) never
    # trip it. The guard does not unwind the already-persisted parks.
    suspended = [result for result in results if isinstance(result, SuspendedInteraction)]
    if len(suspended) > 1:
        raise BatchMultiParkUnsupported(tool_name, [s.interaction_id for s in suspended])
    if suspended:
        # Propagated WHOLE — the park's resume owner rides with the sentinel, so whoever
        # claims it downstream still checks it owns it. Re-minting one here would drop that.
        return suspended[0]
    # Reachable only with zero sentinels, so every element is an ordinary body result.
    return cast("list[Any]", results)
