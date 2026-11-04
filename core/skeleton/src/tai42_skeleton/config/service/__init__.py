"""The single manifest-mutation pipeline — public surface.

Re-exports the pipeline class and its result/outcome DTOs so every consumer imports them
from ``tai42_skeleton.config.service`` unchanged.
"""

from __future__ import annotations

from tai42_skeleton.config.service.deferred_roll import DeferredRolls, deferring_rolls
from tai42_skeleton.config.service.results import (
    ApplyResult,
    FleetReloadOutcome,
    OrphanEnvWriteError,
    PersistedChange,
    ProfileApplyOutcome,
    joined_span_release_errors,
)
from tai42_skeleton.config.service.service import ConfigService

__all__ = [
    "ApplyResult",
    "ConfigService",
    "DeferredRolls",
    "FleetReloadOutcome",
    "OrphanEnvWriteError",
    "PersistedChange",
    "ProfileApplyOutcome",
    "deferring_rolls",
    "joined_span_release_errors",
]
