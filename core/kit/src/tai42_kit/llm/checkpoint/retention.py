"""A checkpoint thread's retention, declared by the thread's owner and bounded by the platform's two values.

The platform's ``checkpoint_retention_waiting_minutes`` and ``checkpoint_retention_finished_minutes``
are every thread's default AND its ceiling: an owner may declare a shorter life for the threads it
mints, never a longer one. A value out of bounds is refused where it is built, never clamped.
"""

from __future__ import annotations

from dataclasses import dataclass

from tai42_kit.llm.settings import llm_provider_settings

_SETTING = "LLM_PROVIDER_CHECKPOINT_RETENTION_{}_MINUTES"


class ThreadRetentionError(ValueError):
    """A declared checkpoint retention is out of bounds; the message names the value and the bound it broke."""


@dataclass(frozen=True)
class ThreadRetention:
    """How long a checkpoint thread is kept, in minutes, bounded by the platform's values.

    ``waiting_minutes``: after its last write, while its owner may still continue it.
    ``finished_minutes``: after its owner marked it finished.

    Every value is checked when it is built, so every seam that takes one (the saver views, the
    finished-thread ledger, the park horizon) holds the platform's ceiling. Raises
    :class:`ThreadRetentionError` for a value that is not positive, a value above the platform's
    for its horizon, or a finished longer than the waiting.
    """

    waiting_minutes: int
    finished_minutes: int

    def __post_init__(self) -> None:
        """Refuse a value out of the platform's bounds, naming the value and the bound it broke."""
        _check_positive("waiting_minutes", self.waiting_minutes)
        _check_positive("finished_minutes", self.finished_minutes)
        settings = llm_provider_settings()
        _check_ceiling("waiting_minutes", self.waiting_minutes, settings.checkpoint_retention_waiting_minutes)
        _check_ceiling("finished_minutes", self.finished_minutes, settings.checkpoint_retention_finished_minutes)
        if self.finished_minutes > self.waiting_minutes:
            raise ThreadRetentionError(
                f"checkpoint retention finished_minutes ({self.finished_minutes} min) must not exceed "
                f"waiting_minutes ({self.waiting_minutes} min)"
            )


def platform_retention() -> ThreadRetention:
    """The platform's two retention settings: the default and the ceiling of every declaration."""
    settings = llm_provider_settings()
    return ThreadRetention(
        waiting_minutes=settings.checkpoint_retention_waiting_minutes,
        finished_minutes=settings.checkpoint_retention_finished_minutes,
    )


def _check_positive(name: str, value: int) -> None:
    if value <= 0:
        raise ThreadRetentionError(f"checkpoint retention {name} ({value} min) must be positive")


def _check_ceiling(name: str, value: int, ceiling: int) -> None:
    if value > ceiling:
        raise ThreadRetentionError(
            f"checkpoint retention {name} ({value} min) exceeds the platform's {ceiling} min "
            f"({_SETTING.format(name.removesuffix('_minutes').upper())})"
        )


def resolve_retention(*, waiting_minutes: int | None = None, finished_minutes: int | None = None) -> ThreadRetention:
    """Resolve an owner's declaration against the platform's values.

    An unset waiting is the platform's; an unset finished is the platform's finished capped at the
    resolved waiting (a finished thread is never kept longer than a waiting one). The resolved
    value is bounded where it is built (:class:`ThreadRetention`), which raises
    :class:`ThreadRetentionError` for a declaration out of bounds.
    """
    platform = platform_retention()
    waiting = platform.waiting_minutes if waiting_minutes is None else waiting_minutes
    if finished_minutes is None:
        finished_minutes = min(platform.finished_minutes, waiting)
    return ThreadRetention(waiting, finished_minutes)
