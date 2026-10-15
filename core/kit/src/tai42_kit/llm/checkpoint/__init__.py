"""Checkpoint savers for LLM graph state: the provider registry, the per-loop registry, and the retention seams.

A checkpoint store is a registration of :class:`CheckpointProvider` (its name, its facts, the hooks the
kit applies its features through); the kit's built-in stores register at this package's import. The
owner-declared thread retention, the finished-thread ledger calls, the live-thread filter registry and
the pieces a store's registration builds on (the shared-value chunker and resolver, the value store
and ledger protocols, the msgpack value encoding) are exported here.
"""

from tai42_kit.llm.checkpoint import stores  # noqa: F401 -- the built-in stores register at import
from tai42_kit.llm.checkpoint.checkpoint import (
    REFERENCING_TYPE_SUFFIX,
    CheckpointResource,
    CheckpointSerializationError,
    PreEncodedCheckpoint,
    PreEncodedValue,
    create_checkpoint_resource,
    create_saver_with_waiting,
    get_saver_from_resource,
)
from tai42_kit.llm.checkpoint.chunking import (
    CheckpointReferenceError,
    CheckpointValueRef,
    SharedSnapshot,
    ValueChunker,
    ValueEncoding,
    resolve_references,
    resolve_tuple,
)
from tai42_kit.llm.checkpoint.envelope import CheckpointCodec, envelope_reference, reference_envelope
from tai42_kit.llm.checkpoint.ledger import (
    FinishedThreadLedger,
    as_utc,
    mark_threads_active,
    mark_threads_finished,
    stale_threads_by_walk,
)
from tai42_kit.llm.checkpoint.liveness import (
    LiveThreadFilter,
    live_thread_filters,
    live_threads,
    register_live_thread_filter,
)
from tai42_kit.llm.checkpoint.providers import (
    ChannelStorage,
    CheckpointProvider,
    CheckpointProviderFacts,
    CheckpointWrite,
    RetentionMode,
    UnknownCheckpointProviderError,
    checkpoint_park_horizon,
    checkpoint_provider,
    checkpoint_provider_facts,
    checkpoint_providers,
    durable_checkpoint_providers,
    register_checkpoint_provider,
    shared_value_threshold,
)
from tai42_kit.llm.checkpoint.retention import (
    ThreadRetention,
    ThreadRetentionError,
    platform_retention,
    resolve_retention,
)
from tai42_kit.llm.checkpoint.store_format import CHECKPOINT_STORE_FORMAT_GENERATION, CheckpointStoreFormatError
from tai42_kit.llm.checkpoint.stores.redis import REDIS_CHECKPOINT_NOT_CONFIGURED_MESSAGE
from tai42_kit.llm.checkpoint.value_encoding import MsgpackValueEncoding
from tai42_kit.llm.checkpoint.value_store import CheckpointValueStore, StoredBlob

__all__ = [
    "CHECKPOINT_STORE_FORMAT_GENERATION",
    "REDIS_CHECKPOINT_NOT_CONFIGURED_MESSAGE",
    "REFERENCING_TYPE_SUFFIX",
    "ChannelStorage",
    "CheckpointCodec",
    "CheckpointProvider",
    "CheckpointProviderFacts",
    "CheckpointReferenceError",
    "CheckpointResource",
    "CheckpointSerializationError",
    "CheckpointStoreFormatError",
    "CheckpointValueRef",
    "CheckpointValueStore",
    "CheckpointWrite",
    "FinishedThreadLedger",
    "LiveThreadFilter",
    "MsgpackValueEncoding",
    "PreEncodedCheckpoint",
    "PreEncodedValue",
    "RetentionMode",
    "SharedSnapshot",
    "StoredBlob",
    "ThreadRetention",
    "ThreadRetentionError",
    "UnknownCheckpointProviderError",
    "ValueChunker",
    "ValueEncoding",
    "as_utc",
    "checkpoint_park_horizon",
    "checkpoint_provider",
    "checkpoint_provider_facts",
    "checkpoint_providers",
    "create_checkpoint_resource",
    "create_saver_with_waiting",
    "durable_checkpoint_providers",
    "envelope_reference",
    "get_saver_from_resource",
    "live_thread_filters",
    "live_threads",
    "mark_threads_active",
    "mark_threads_finished",
    "platform_retention",
    "reference_envelope",
    "register_checkpoint_provider",
    "register_live_thread_filter",
    "resolve_references",
    "resolve_retention",
    "resolve_tuple",
    "shared_value_threshold",
    "stale_threads_by_walk",
]
