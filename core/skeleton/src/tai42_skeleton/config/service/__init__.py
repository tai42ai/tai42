"""The single manifest-mutation pipeline — public surface.

Re-exports the pipeline class and its result/outcome DTOs so every consumer imports them
from ``tai42_skeleton.config.service`` unchanged.
"""

from __future__ import annotations

from tai42_skeleton.config.service.results import ApplyResult, OrphanEnvWriteError, ProfileApplyOutcome
from tai42_skeleton.config.service.service import ConfigService

__all__ = ["ApplyResult", "ConfigService", "OrphanEnvWriteError", "ProfileApplyOutcome"]
