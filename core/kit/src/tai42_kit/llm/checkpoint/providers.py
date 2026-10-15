"""The checkpoint provider registry: each store registers its facts and the hooks the kit applies its features through.

A store is a :class:`CheckpointProvider` subclass registered with :func:`register_checkpoint_provider`
at its module's import; ``LLM_PROVIDER_CHECKPOINT`` (or a run's own ``checkpoint_provider``) names
the registration a saver is built from. The kit reads a store's three facts and nothing else about it:
whether a parked run survives a restart, how expired threads leave the store, and how its saver stores
a channel value. :func:`shared_value_threshold` is the one place the store's facts and the deployment's
``LLM_PROVIDER_CHECKPOINT_SHARED_VALUES`` meet.

The registry is plain module state, filled at import. Registering the same class again (a module
re-executed by a reload) changes nothing; a removed registering module keeps its registration until
the process restarts.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, get_args

from tai42_kit.llm.settings import llm_provider_settings
from tai42_kit.registry.staged import NamedFactoryRegistry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence

    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver, ChannelVersions, Checkpoint

    from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource
    from tai42_kit.llm.checkpoint.chunking import ValueChunker, ValueEncoding
    from tai42_kit.llm.checkpoint.envelope import CheckpointCodec
    from tai42_kit.llm.checkpoint.retention import ThreadRetention
    from tai42_kit.llm.checkpoint.value_store import CheckpointValueStore

RetentionMode = Literal["native_ttl", "sweep", "process"]
ChannelStorage = Literal["per_checkpoint", "per_version"]

_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]*$")


class UnknownCheckpointProviderError(ValueError):
    """A checkpoint provider name no registration holds."""


@dataclass(frozen=True)
class CheckpointProviderFacts:
    """What the kit reads about one checkpoint store."""

    # A parked run survives a process restart.
    durable: bool
    # How expired threads leave the store: the saver's own key TTL, the platform sweep,
    # or the end of the process that holds them.
    retention: RetentionMode
    # How the saver stores a channel value: copied into every checkpoint that carries it,
    # or once per version of the channel.
    channel_storage: ChannelStorage

    def __post_init__(self) -> None:
        """Refuse a value outside its kind, and a durable store whose threads end with the process."""
        if type(self.durable) is not bool:
            raise ValueError(f"checkpoint provider fact durable must be a bool, got {self.durable!r}")
        if self.retention not in get_args(RetentionMode):
            raise ValueError(
                f"checkpoint provider fact retention must be one of {', '.join(get_args(RetentionMode))}, "
                f"got {self.retention!r}"
            )
        if self.channel_storage not in get_args(ChannelStorage):
            raise ValueError(
                f"checkpoint provider fact channel_storage must be one of {', '.join(get_args(ChannelStorage))}, "
                f"got {self.channel_storage!r}"
            )
        if self.durable and self.retention == "process":
            raise ValueError("a checkpoint store whose threads end with the process is not durable")


@dataclass(frozen=True)
class CheckpointWrite:
    """What the kit hands a store's write preparation: the thread's value store and the kit's tools.

    ``saver`` is the store's own saver, ``values`` the value store the write's large values go to,
    ``encoding`` the store's value encoding and ``threshold`` the encoded size above which a value is
    stored once (a preparation runs only where sharing is on).
    """

    saver: BaseCheckpointSaver
    config: RunnableConfig
    thread_id: str
    values: CheckpointValueStore
    encoding: ValueEncoding
    threshold: int

    def chunker(self) -> ValueChunker:
        """A chunker over the store's encoding and the threshold."""
        from tai42_kit.llm.checkpoint.chunking import ValueChunker

        return ValueChunker(self.encoding, self.threshold)

    async def store(self, digests: Collection[str], blobs: Mapping[str, tuple[str, bytes]]) -> None:
        """Re-arm every digest of ``digests`` and store the missing ones from ``blobs``, in their store form.

        A digest the value store does not hold and ``blobs`` does not encode raises
        :class:`~tai42_kit.llm.checkpoint.chunking.CheckpointReferenceError` naming the thread and the digest.
        """
        if not digests:
            return
        import asyncio

        from tai42_kit.llm.checkpoint.chunking import CheckpointReferenceError

        missing = await self.values.touch(self.thread_id, digests)
        if not missing:
            return
        unknown = sorted(digest for digest in missing if digest not in blobs)
        if unknown:
            raise CheckpointReferenceError(
                f"checkpoint thread {self.thread_id!r} references a value {unknown[0]} it does not hold and this "
                "write does not encode"
            )
        encoding = self.encoding
        stored = await asyncio.to_thread(lambda: {digest: encoding.store_form(blobs[digest]) for digest in missing})
        await self.values.put(self.thread_id, stored)


class CheckpointProvider(ABC):
    """One checkpoint store's registration: its name, its facts, and the hooks the kit applies its features through.

    The kit calls the hooks and applies everything else itself, the same way for every store: the
    serialization guard, large values stored once per thread, the store-format gate, the retention
    views and the sweep. A hook receives the settings it needs as arguments.
    """

    # The ``LLM_PROVIDER_CHECKPOINT`` value: lowercase letters, digits, ``_`` and ``-``, a letter first.
    name: ClassVar[str]
    facts: ClassVar[CheckpointProviderFacts]

    @abstractmethod
    async def open(self, conn_string: str | None) -> tuple[CheckpointResource, Callable[[], Awaitable[None]]]:
        """Open the store: its handle, its finished-thread ledger and its per-thread value store, set up.

        Returns the resource and the function that closes it. A ``None`` conn string means the store's
        default connection, or raises naming the setting to set.
        """

    @abstractmethod
    def saver(self, resource: CheckpointResource) -> BaseCheckpointSaver:
        """The store's saver over ``resource``, as the library builds it; the kit guards and wraps it."""

    async def saver_with_waiting(
        self, resource: CheckpointResource, waiting_minutes: int
    ) -> tuple[BaseCheckpointSaver, CheckpointValueStore]:
        """A saver over ``resource`` whose writes carry ``waiting_minutes`` as their key TTL, with its value store.

        The value store's writes carry the same TTL over the store's own key space. Required when the
        store's retention is ``native_ttl``.
        """
        raise NotImplementedError(f"the {self.name!r} checkpoint provider has no saver with its own waiting")

    def serializer(
        self, saver: BaseCheckpointSaver, compress_threshold: int | None
    ) -> tuple[Any, CheckpointCodec | None]:
        """The serializer the kit guards on ``saver`` and the codec it installs around it.

        ``compress_threshold`` is ``LLM_PROVIDER_CHECKPOINT_INLINE_COMPRESS_BYTES``. Default: the
        saver's own serializer and no codec.
        """
        return saver.serde, None

    @abstractmethod
    def value_encoding(self, saver: BaseCheckpointSaver, compress_threshold: int | None) -> ValueEncoding:
        """How this store encodes a value for the chunker, its value store and the resolver.

        ``saver`` carries the guarded serializer.
        """

    async def prepare_checkpoint(
        self, write: CheckpointWrite, checkpoint: Checkpoint, new_versions: ChannelVersions
    ) -> tuple[Checkpoint, ChannelVersions]:
        """The checkpoint and versions the saver is handed; every value they reference is held when this returns.

        Default: the kit's preparation for :attr:`facts`' ``channel_storage``. A store overrides it
        only with knowledge of its own saver's storage.
        """
        from tai42_kit.llm.checkpoint import shared_values

        return await shared_values.prepare_checkpoint(write, self.facts.channel_storage, checkpoint, new_versions)

    async def prepare_writes(
        self, write: CheckpointWrite, writes: Sequence[tuple[str, Any]]
    ) -> Sequence[tuple[str, Any]]:
        """The pending writes the saver is handed; every value they reference is held when this returns.

        Default: every write value chunked and pre-encoded.
        """
        from tai42_kit.llm.checkpoint import shared_values

        return await shared_values.prepare_writes(write, writes)

    @abstractmethod
    async def recorded_generation(self, resource: CheckpointResource) -> int | None:
        """The store-format generation the store records, or ``None`` when it records none."""

    @abstractmethod
    async def record_generation(self, resource: CheckpointResource, generation: int) -> None:
        """Record ``generation`` unless one is recorded: concurrent first builds keep the first."""

    @abstractmethod
    async def holds_checkpoints(self, resource: CheckpointResource) -> bool:
        """Whether the store holds any checkpoint."""


_PROVIDERS: NamedFactoryRegistry[type[CheckpointProvider]] = NamedFactoryRegistry("Checkpoint provider")
_instances: dict[str, CheckpointProvider] = {}


def register_checkpoint_provider[P: type[CheckpointProvider]](cls: P) -> P:
    """Register ``cls`` under its ``name``; usable as a class decorator.

    Raises ``TypeError`` for a class that is not a :class:`CheckpointProvider`, misses a hook or
    declares facts that are not :class:`CheckpointProviderFacts`, and ``ValueError`` for a malformed
    name, a ``native_ttl`` store without :meth:`~CheckpointProvider.saver_with_waiting`, or a name
    another class holds. The same class registered again is a no-op.
    """
    if not (isinstance(cls, type) and issubclass(cls, CheckpointProvider)):
        raise TypeError(f"a checkpoint provider registration is a CheckpointProvider subclass, got {cls!r}")
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"checkpoint provider name {name!r} of {cls.__qualname__} must match {_NAME_PATTERN.pattern}")
    facts = getattr(cls, "facts", None)
    if not isinstance(facts, CheckpointProviderFacts):
        raise TypeError(f"checkpoint provider {name!r} declares facts {facts!r}, not CheckpointProviderFacts")
    if facts.retention == "native_ttl" and cls.saver_with_waiting is CheckpointProvider.saver_with_waiting:
        raise ValueError(
            f"checkpoint provider {name!r} keeps threads by its keys' TTL (retention native_ttl) "
            "but defines no saver_with_waiting"
        )
    instance = cls()

    def _hold() -> None:
        _instances[name] = instance

    _PROVIDERS.register(name, cls, before_add=_hold)
    return cls


def checkpoint_provider(name: str) -> CheckpointProvider:
    """The registration of ``name``; an unknown name raises :class:`UnknownCheckpointProviderError`."""
    try:
        _PROVIDERS.get(name)
    except KeyError:
        known = ", ".join(registered for registered, _cls in _PROVIDERS.items())
        raise UnknownCheckpointProviderError(f"Unsupported checkpoint provider: {name!r}; known: {known}") from None
    return _instances[name]


def checkpoint_providers() -> Mapping[str, CheckpointProviderFacts]:
    """A read-only, name-sorted view of every registered provider's facts."""
    return MappingProxyType({name: cls.facts for name, cls in _PROVIDERS.items()})


def checkpoint_provider_facts(provider: str) -> CheckpointProviderFacts:
    """Return the facts of ``provider``; an unknown name raises :class:`UnknownCheckpointProviderError`."""
    return checkpoint_provider(provider).facts


def durable_checkpoint_providers() -> frozenset[str]:
    """The providers whose checkpoints survive a process restart."""
    return frozenset(name for name, cls in _PROVIDERS.items() if cls.facts.durable)


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

    ``None`` when every value is stored inline. ``LLM_PROVIDER_CHECKPOINT_SHARED_VALUES`` decides when
    it is set; unset, a store that copies channel values into every checkpoint stores them once, and a
    store that keeps a channel once per version does not.
    """
    facts = checkpoint_provider_facts(provider)
    settings = llm_provider_settings()
    switch = settings.checkpoint_shared_values
    on = facts.channel_storage == "per_checkpoint" if switch is None else switch
    return settings.checkpoint_shared_value_bytes if on else None
