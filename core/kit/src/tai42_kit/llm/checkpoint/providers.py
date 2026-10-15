"""The checkpoint provider table: what each provider is, and the retention horizons derived from it.

Every fact about a checkpoint provider — whether a parked run survives a process restart,
how expired threads leave its store, which codec the kit installs on its saver, whether its
saver keeps a per-thread value store and whether it stores large values once per thread by
default — is read from this one table. :func:`shared_value_threshold` is the one place the
provider's default and the deployment's ``LLM_PROVIDER_CHECKPOINT_SHARED_VALUES`` meet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from tai42_kit.llm.settings import llm_provider_settings

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tai42_kit.llm.checkpoint.retention import ThreadRetention

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
    # The saver keeps a per-thread value store beside its own keys or tables.
    value_store: bool
    # Large sub-values are stored once per thread unless the deployment's
    # LLM_PROVIDER_CHECKPOINT_SHARED_VALUES says otherwise; never without a value store.
    shared_values: bool

    def __post_init__(self) -> None:
        """Refuse a provider that stores values once by default without a value store."""
        if self.shared_values and not self.value_store:
            raise ValueError("a checkpoint provider that stores values once by default but keeps no value store")


class CheckpointSharedValuesError(ValueError):
    """The deployment asks a checkpoint provider whose saver keeps no value store to store values once."""


CHECKPOINT_PROVIDERS: Final[Mapping[str, CheckpointProviderFacts]] = MappingProxyType(
    {
        "redis": CheckpointProviderFacts(
            durable=True,
            retention="native_ttl",
            faithful_codec=True,
            inline_compress=True,
            value_store=True,
            shared_values=True,
        ),
        "postgres": CheckpointProviderFacts(
            durable=True,
            retention="sweep",
            faithful_codec=False,
            inline_compress=True,
            value_store=True,
            shared_values=False,
        ),
        "sqlite": CheckpointProviderFacts(
            durable=False,
            retention="sweep",
            faithful_codec=False,
            inline_compress=False,
            value_store=False,
            shared_values=False,
        ),
        "memory": CheckpointProviderFacts(
            durable=False,
            retention="process",
            faithful_codec=False,
            inline_compress=False,
            value_store=False,
            shared_values=False,
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


def checkpoint_park_horizon(provider: str, retention: ThreadRetention | None = None) -> timedelta | None:
    """How long a parked run's checkpoint is kept on ``provider``; ``None`` when the store does not outlive the process.

    On a durable provider a parked thread's last write is its park checkpoint, and the thread is kept its
    waiting retention after it — the owner's declared one, else the platform's — so a park may wait at
    most that long.
    """
    if not checkpoint_provider_facts(provider).durable:
        return None
    if retention is not None:
        return timedelta(minutes=retention.waiting_minutes)
    return timedelta(minutes=llm_provider_settings().checkpoint_retention_waiting_minutes)


def shared_value_threshold(provider: str) -> int | None:
    """The encoded size above which ``provider`` stores a checkpoint sub-value once per thread.

    ``None`` when every value is stored inline.
    ``LLM_PROVIDER_CHECKPOINT_SHARED_VALUES`` decides when it is set, else the provider's own default;
    ``true`` on a provider whose saver keeps no value store raises :class:`CheckpointSharedValuesError`.
    """
    facts = checkpoint_provider_facts(provider)
    settings = llm_provider_settings()
    on = facts.shared_values if settings.checkpoint_shared_values is None else settings.checkpoint_shared_values
    if on and not facts.value_store:
        raise CheckpointSharedValuesError(
            f"LLM_PROVIDER_CHECKPOINT_SHARED_VALUES=true: the {provider!r} checkpoint provider keeps no value "
            "store, so it cannot store values once per thread; unset it or set it false"
        )
    return settings.checkpoint_shared_value_bytes if on else None
