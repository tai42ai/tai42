"""Writes and deletes of the template store that keep the render state consistent with it.

Every write evicts the render state of what it replaced, so the next render reads the new
content; every delete first refuses an object a stored object still names.
"""

import logging
from collections.abc import Callable

from tai42_contract.storage import Storage, assert_not_root

logger = logging.getLogger(__name__)


class StoreWrites:
    """Upload and delete stored objects, evicting what each write made stale.

    ``storage`` returns the active Storage provider, raising when none is registered;
    ``evict_id`` drops the render state of one id and ``evict_dir`` of everything under a
    directory.
    """

    def __init__(
        self,
        storage: Callable[[], Storage],
        *,
        evict_id: Callable[[str], None],
        evict_dir: Callable[[str], None],
    ) -> None:
        """Write through the provider ``storage`` returns, evicting through the two eviction seams."""
        self._storage = storage
        self._evict_id = evict_id
        self._evict_dir = evict_dir

    async def upload_template(self, path: str, content: str) -> None:
        """Store ``content`` at ``path`` and evict its compiled entry.

        The compiled-template cache keys entries by ``path`` and holds them until
        the cache TTL, so the single key is evicted after a successful upload to
        keep renders consistent with storage — the next render recompiles the new
        content.
        """
        await self._storage().upload(path, content)
        self._evict_id(path)

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        """Store raw ``data`` at ``path`` with ``content_type`` and evict its compiled entry.

        The bytes twin of :meth:`upload_template`. A text-only provider refuses bytes
        that are not UTF-8 with ``UnicodeDecodeError``, which propagates.
        """
        await self._storage().upload_bytes(path, data, content_type)
        self._evict_id(path)

    async def delete_object(self, path: str) -> None:
        """Delete the stored object at ``path`` unless a stored object still names it.

        Every delete of a stored object goes through here or :meth:`delete_template_dir`.
        A held object raises the ``409`` naming its holders and deletes nothing; a holder
        that cannot be read raises a ``500``; a missing object raises ``FileNotFoundError``.
        """
        from tai42_skeleton.operations.holders import refuse_held_templates  # operations sit above this layer

        await refuse_held_templates(frozenset({path}), head=f"stored template '{path}' cannot be deleted")
        await self._storage().delete(path)

    async def delete_template(self, path: str) -> None:
        """Delete the stored template at ``path`` and evict its compiled entry.

        Idempotent: an already-absent template is a no-op. This is the one seam
        every backend flows through, so the backends' ``FileNotFoundError`` for a
        missing object is mapped to the documented no-op here rather than in each
        backend. A held template is refused by :meth:`delete_object`.
        """
        try:
            await self.delete_object(path)
        except FileNotFoundError:
            logger.info("delete_template: %s already absent; delete is a no-op", path)
        # The compiled cache retains an entry per path until TTL; evict only the
        # deleted key so its stale compilation is dropped while other templates
        # stay cached.
        self._evict_id(path)

    async def delete_template_dir(self, path: str) -> None:
        """Delete every stored template under ``path/`` and evict their compiled entries.

        A ``path`` naming the store root raises ``ValueError`` before any read. Only the
        directory's objects are listed (``Storage.list(path)``), never the whole store.
        Refused, deleting and evicting nothing, when a stored object names any object under
        ``path/``. Eviction runs even on a partial-delete failure.
        """
        from tai42_skeleton.operations.holders import refuse_held_templates  # operations sit above this layer

        assert_not_root(path)
        storage = self._storage()
        ids = frozenset(await storage.list(path))
        await refuse_held_templates(ids, head=f"directory '{path}' cannot be deleted")
        try:
            return await storage.delete_dir(path)
        finally:
            # Dir-delete is non-atomic: a failure can still leave some files
            # deleted, so evict regardless of outcome — never serve content under
            # this dir after a partial delete. Only keys under the prefix are
            # dropped; templates outside it stay cached.
            self._evict_dir(path)
