"""The ``postgres`` checkpoint store: LangGraph's async Postgres saver on a dedicated pool.

The saver stores a changed channel as a blob of its version. Beside its tables the kit keeps its
finished-thread ledger, its declared retentions, its value table ``tai42_checkpoint_values`` and its
store-format marker (:mod:`tai42_kit.llm.checkpoint.postgres_store`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from tai42_kit.llm.checkpoint import postgres_store
from tai42_kit.llm.checkpoint.ledger import as_utc
from tai42_kit.llm.checkpoint.providers import (
    CheckpointProvider,
    CheckpointProviderFacts,
    CheckpointWrite,
    register_checkpoint_provider,
)
from tai42_kit.llm.checkpoint.value_store import StoredBlob

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
    from datetime import datetime

    from langgraph.checkpoint.base import BaseCheckpointSaver, ChannelVersions, Checkpoint
    from psycopg import AsyncConnection
    from psycopg.rows import DictRow
    from psycopg_pool import AsyncConnectionPool

    from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource
    from tai42_kit.llm.checkpoint.envelope import CheckpointCodec
    from tai42_kit.llm.checkpoint.retention import ThreadRetention
    from tai42_kit.llm.checkpoint.value_encoding import MsgpackValueEncoding


class PostgresFinishedThreadLedger:
    """The ledger of a Postgres checkpoint store, two tables beside the saver's tables."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """``pool``: the store's psycopg pool."""
        self._pool = pool

    async def setup(self) -> None:
        """Create the ledger tables beside the saver's tables."""
        await postgres_store.create_ledger_tables(self._pool)

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        """Remove the finished marks of ``thread_ids`` and record (or, for ``None``, remove) their waiting."""
        await postgres_store.start_threads(
            self._pool, thread_ids, None if retention is None else retention.waiting_minutes
        )

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        """Record ``thread_ids`` as finished, due at ``due_at``."""
        await postgres_store.mark_finished(self._pool, thread_ids, as_utc(due_at))

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids due at or before ``cutoff``, earliest first."""
        return await postgres_store.finished_before(self._pool, as_utc(cutoff), limit)

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        await postgres_store.forget_threads(self._pool, thread_ids)

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        """The recorded declared waiting of those of ``thread_ids`` that have one."""
        return await postgres_store.declared_waiting(self._pool, thread_ids)

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """The threads past their waiting horizon, from one query over the store's own tables; ``saver`` is unused."""
        return await postgres_store.stale_threads(self._pool, now=as_utc(now), default_minutes=default_minutes)


class PostgresCheckpointValueStore:
    """Shared values as rows of the kit's value table, deleted with their thread."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store through ``pool``."""
        self._pool = pool

    async def setup(self) -> None:
        """Create the value table beside the saver's tables."""
        await postgres_store.create_value_table(self._pool)

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        """The digests the table does not hold for ``thread_id`` (a row has no retention to re-arm)."""
        wanted = set(digests)
        return wanted - await postgres_store.held_values(self._pool, thread_id, wanted)

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        """Insert every blob; a digest already held keeps its row."""
        if blobs:
            await postgres_store.insert_values(
                self._pool, thread_id, [(digest, blob.type, blob.data) for digest, blob in blobs.items()]
            )

    async def get(self, thread_id: str, digests: Collection[str]) -> dict[str, StoredBlob]:
        """The held blobs among ``digests``; one statement."""
        rows = await postgres_store.read_values(self._pool, thread_id, set(digests))
        return {digest: StoredBlob(type_, data) for digest, type_, data in rows}

    async def held_digests(self, thread_id: str) -> set[str]:
        """Every digest ``thread_id`` holds."""
        return await postgres_store.thread_values(self._pool, thread_id)

    async def delete_thread(self, thread_id: str) -> None:
        """Delete every row of ``thread_id``."""
        await postgres_store.delete_values(self._pool, thread_id)


@register_checkpoint_provider
class PostgresCheckpointProvider(CheckpointProvider):
    """LangGraph's ``AsyncPostgresSaver`` on a dedicated pool; the saver stores a channel once per version."""

    name: ClassVar[str] = "postgres"
    facts: ClassVar[CheckpointProviderFacts] = CheckpointProviderFacts(
        durable=True, retention="sweep", channel_storage="per_version"
    )

    async def open(self, conn_string: str | None) -> tuple[CheckpointResource, Callable[[], Awaitable[None]]]:
        """Open the pool and set up the saver's and the kit's tables.

        ``None`` opens the base Postgres namespace (``PG_*``), whose DSN builder raises a named error
        when its identity is unset.
        """
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # pyright: ignore[reportMissingImports]

        from tai42_kit.clients.impl.postgres import open_dedicated_pool
        from tai42_kit.clients.settings import PostgresConnectionSettings
        from tai42_kit.llm._langgraph_postgres import LANGGRAPH_CONNECTION_KWARGS, serialised_setup
        from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource

        target: PostgresConnectionSettings | str = PostgresConnectionSettings() if conn_string is None else conn_string
        pool = await open_dedicated_pool(target, role="checkpoint", connection_kwargs=dict(LANGGRAPH_CONNECTION_KWARGS))
        ledger = PostgresFinishedThreadLedger(pool)
        values = PostgresCheckpointValueStore(pool)
        try:
            # The pool's per-connection kwargs set row_factory=dict_row, so every checkout
            # yields dict rows; narrow the row-agnostic pool type to the saver's dict-row connection.
            async with pool.connection() as conn, serialised_setup(conn, "checkpoint_migrations"):
                await AsyncPostgresSaver(cast("AsyncConnection[DictRow]", conn)).setup()
            await values.setup()
            await ledger.setup()
        except BaseException:
            # No cleanup function is returned on this path: close the pool just opened.
            await pool.close()
            raise

        async def close() -> None:
            await pool.close()

        return CheckpointResource(self.name, pool, ledger, values), close

    def saver(self, resource: CheckpointResource) -> BaseCheckpointSaver:
        """An ``AsyncPostgresSaver`` on the pool."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # pyright: ignore[reportMissingImports]

        return AsyncPostgresSaver(resource.handle)

    def serializer(
        self, saver: BaseCheckpointSaver, compress_threshold: int | None
    ) -> tuple[Any, CheckpointCodec | None]:
        """The saver's serializer, its blobs compressed above ``compress_threshold``."""
        from tai42_kit.llm.checkpoint.envelope import BlobCompressCodec

        return saver.serde, BlobCompressCodec(compress_threshold)

    def value_encoding(self, saver: BaseCheckpointSaver, compress_threshold: int | None) -> MsgpackValueEncoding:
        """The saver's msgpack, compressed above ``compress_threshold``."""
        from tai42_kit.llm.checkpoint.value_encoding import MsgpackValueEncoding

        return MsgpackValueEncoding(saver.serde, compress_threshold)

    async def prepare_checkpoint(
        self, write: CheckpointWrite, checkpoint: Checkpoint, new_versions: ChannelVersions
    ) -> tuple[Checkpoint, ChannelVersions]:
        """The kit's per-version preparation, with the saver's own rule for primitive channels.

        The saver keeps a primitive channel value (``None``, a string, a number, a bool) inline in
        every checkpoint row whatever its version, and a changed channel of any other value as a blob
        of its version. A string above the threshold is therefore handed over as its reference blob on
        every checkpoint, with its current version: the saver writes that version's blob when it lacks
        one and keeps the string out of the row. Other primitives stay inline.
        """
        import asyncio

        from langgraph.checkpoint.serde.types import _DeltaSnapshot

        from tai42_kit.llm.checkpoint.checkpoint import PreEncodedValue

        values = checkpoint["channel_values"]
        current = checkpoint["channel_versions"]

        def encode() -> tuple[dict[str, Any], dict[str, Any], Mapping[str, tuple[str, bytes]]]:
            chunker = write.chunker()
            channels: dict[str, Any] = {}
            versions: dict[str, Any] = dict(new_versions)
            for name, value in values.items():
                if isinstance(value, str) and name in current:
                    rewritten, encoded = chunker.chunk(value)
                    if rewritten is not value:
                        channels[name] = PreEncodedValue(write.inline_form(encoded), references=True)
                        versions.setdefault(name, current[name])
                    continue
                if name not in new_versions or value is None or isinstance(value, (str, int, float, bool)):
                    continue
                if isinstance(value, _DeltaSnapshot):
                    # The saver marks a snapshot by its type: it stays one, its payload chunked in place.
                    channels[name], _encoded = chunker.chunk(value)
                    continue
                rewritten, encoded = chunker.chunk(value)
                channels[name] = PreEncodedValue(write.inline_form(encoded), references=rewritten is not value)
            return channels, versions, chunker.blobs

        channels, versions, blobs = await asyncio.to_thread(encode)
        await write.store(set(blobs), blobs)
        prepared = cast("Checkpoint", {**checkpoint, "channel_values": {**values, **channels}})
        return prepared, cast("ChannelVersions", versions)

    async def recorded_generation(self, resource: CheckpointResource) -> int | None:
        """The generation the marker table records."""
        return await postgres_store.read_format_generation(resource.handle)

    async def record_generation(self, resource: CheckpointResource, generation: int) -> None:
        """Record ``generation`` unless one is recorded."""
        await postgres_store.write_format_generation(resource.handle, generation)

    async def holds_checkpoints(self, resource: CheckpointResource) -> bool:
        """Whether the saver's ``checkpoints`` table holds a row."""
        return await postgres_store.holds_checkpoints(resource.handle)
