"""The per-thread value store a checkpoint store keeps its shared values in, beside its checkpoints.

A value lives as long as its thread: the store never deletes one while the thread lives (any older
checkpoint of the thread may reference it) and deletes them all with the thread. A store whose
threads expire by their keys' TTL stamps that TTL on its values too, re-armed by every write that
references them, so a value expires no earlier than the last checkpoint referencing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """A value as its thread's value store holds it: a type tag and the stored bytes."""

    type: str
    data: bytes


class CheckpointValueStore(Protocol):
    """Where one checkpoint store keeps its threads' shared values."""

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        """Re-arm the retention of the held ``digests`` of ``thread_id``; return those it does not hold."""
        ...

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        """Store ``blobs`` (by digest) for ``thread_id``."""
        ...

    async def get(self, thread_id: str, digests: Collection[str]) -> dict[str, StoredBlob]:
        """The held blobs among ``digests`` of ``thread_id``; a digest it does not hold is absent."""
        ...

    async def held_digests(self, thread_id: str) -> set[str]:
        """Every digest ``thread_id`` holds."""
        ...

    async def delete_thread(self, thread_id: str) -> None:
        """Delete every value ``thread_id`` holds."""
        ...
