"""The single manifest-mutation pipeline — public surface.

Re-exports the pipeline class, its result/outcome DTOs and the orphan-env-write error (defined
in the leaf :mod:`tai42_skeleton.config.errors`) so every consumer imports them from
``tai42_skeleton.config.service``.
"""

from __future__ import annotations

from tai42_skeleton.config.errors import OrphanEnvWriteError
from tai42_skeleton.config.service.deferred_roll import DeferredRolls, deferring_rolls
from tai42_skeleton.config.service.results import (
    ApplyResult,
    FleetReloadOutcome,
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
