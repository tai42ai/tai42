"""The checkpoint provider table: what each provider is, and the retention horizons derived from it.

Every fact about a checkpoint provider — whether a parked run survives a process restart,
how expired threads leave its store, which codec the kit installs on its saver — is read
from this one table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from tai42_kit.llm.settings import llm_provider_settings

if TYPE_CHECKING:
    from collections.abc import Mapping

RetentionMode = Literal["native_ttl", "sweep", "process"]


class UnknownCheckpointProviderError(ValueError):
    """A checkpoint provider name the provider table does not know."""


@dataclass(frozen=True)
class CheckpointProviderFacts:
    """What the kit knows about one checkpoint provider."""

    # A parked run survives a process restart.
    durable: bool
    # How expired threads leave the store: the saver's own key TTL, the platform sweep,
    # or the end of the process that holds them.
    retention: RetentionMode
    # The kit installs the faithful JSON codec on the saver.
    faithful_codec: bool
    # Large checkpoint values are compressed inline.
    inline_compress: bool


CHECKPOINT_PROVIDERS: Final[Mapping[str, CheckpointProviderFacts]] = MappingProxyType(
    {
        "redis": CheckpointProviderFacts(
            durable=True, retention="native_ttl", faithful_codec=True, inline_compress=True
        ),
        "postgres": CheckpointProviderFacts(
            durable=True, retention="sweep", faithful_codec=False, inline_compress=True
        ),
        "sqlite": CheckpointProviderFacts(
            durable=False, retention="sweep", faithful_codec=False, inline_compress=False
        ),
        "memory": CheckpointProviderFacts(
            durable=False, retention="process", faithful_codec=False, inline_compress=False
        ),
    }
)


def checkpoint_provider_facts(provider: str) -> CheckpointProviderFacts:
    """Return the facts of ``provider``; an unknown name raises :class:`UnknownCheckpointProviderError`."""
    facts = CHECKPOINT_PROVIDERS.get(provider)
    if facts is None:
        known = ", ".join(sorted(CHECKPOINT_PROVIDERS))
        raise UnknownCheckpointProviderError(f"Unsupported checkpoint provider: {provider!r}; known: {known}")
    return facts


def durable_checkpoint_providers() -> frozenset[str]:
    """The providers whose checkpoints survive a process restart."""
    return frozenset(name for name, facts in CHECKPOINT_PROVIDERS.items() if facts.durable)


def checkpoint_park_horizon(provider: str) -> timedelta | None:
    """How long a parked run's checkpoint is kept on ``provider``; ``None`` when the store does not outlive the process.

    On a durable provider a parked thread's last write is its park checkpoint, and the thread is kept
    ``checkpoint_retention_waiting_minutes`` after it, so a park may wait at most that long.
    """
    if not checkpoint_provider_facts(provider).durable:
        return None
    return timedelta(minutes=llm_provider_settings().checkpoint_retention_waiting_minutes)
