"""A preset's output stage: the output map, then the output schema, over the value a preset call returns.

One rule for every place a preset's result is produced: the live call and each place a parked
call's result reappears after the resume.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from tai42_contract.interactions import RunFailed
from tai42_contract.presets import PresetOutputStage
from tai42_contract.secrets import contains_secrets, unwrap_secrets

from tai42_kit.utils.data import run_jq_first
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError, validate_against_json_schema

logger = logging.getLogger(__name__)

__all__ = ["PresetOutputError", "run_output_stage", "run_output_stages"]


class PresetOutputError(Exception):
    """A preset's output stage refused a result.

    The message names the preset and the failed step; it never quotes jq error text or a secret.
    ``preset`` is the refusing preset's name.
    """

    def __init__(self, message: str, *, preset: str) -> None:
        """Build the refusal ``message`` for ``preset``."""
        super().__init__(message)
        self.preset = preset


async def run_output_stage(stage: PresetOutputStage, value: Any, *, carries_secret: bool = False) -> Any:
    """Apply ``stage`` to ``value``: the output map, then the output schema; return the result.

    A :class:`~tai42_contract.interactions.RunFailed` is the run's failure and is returned
    untouched. A value that carries a secret (``carries_secret``, or a
    :class:`~tai42_contract.secrets.SecretValue` inside it) is never mapped, and its schema
    mismatch is reported without the value. Every refusal raises :class:`PresetOutputError`
    with nothing chained. Without a map the input value is returned unchanged (a sealed secret
    stays sealed).
    """
    if isinstance(value, RunFailed):
        return value
    secret = carries_secret or contains_secrets(value)
    if stage.output_map is not None:
        if secret:
            raise PresetOutputError(
                f"preset {stage.preset!r} output map cannot run over a result that carries a secret",
                preset=stage.preset,
            )
        try:
            value = await run_jq_first(stage.output_map, value)
        except (ValueError, TypeError, TimeoutError) as exc:
            logger.warning("preset %r output map failed: %s", stage.preset, exc)
            raise PresetOutputError(f"preset {stage.preset!r} output map failed", preset=stage.preset) from None
    if stage.output_schema is not None:
        text: str | None = None
        try:
            validate_against_json_schema(unwrap_secrets(value), stage.output_schema, redact_value=secret)
        except JsonSchemaValidationError as exc:
            text = str(exc)
        if text is not None:
            raise PresetOutputError(
                f"preset {stage.preset!r} output does not match its output_schema: {text}", preset=stage.preset
            )
    return value


async def run_output_stages(stages: Sequence[PresetOutputStage], value: Any) -> Any:
    """Apply each of ``stages`` in order; the first :class:`PresetOutputError` stops the run of stages."""
    for stage in stages:
        value = await run_output_stage(stage, value)
    return value
