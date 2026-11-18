"""Writes and deletes of the template store that keep the render state consistent with it.

Every write evicts the render state of what it replaced, so the next render reads the new
content; every delete first refuses an object a stored object still names.
"""

import logging
from collections.abc import Awaitable, Callable

from tai42_contract.errors import OperationError
from tai42_contract.storage import Storage, assert_not_root
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.template.errors import HolderUnreadableError, PartialDirectoryDeleteError, StoredObjectHeldError

logger = logging.getLogger(__name__)


class StoreWrites:
    """Upload and delete stored objects, evicting what each write made stale.

    ``storage`` returns the active Storage provider, raising when none is registered;
    ``held_by`` answers every holder of a set of stored ids (the composition layer's referee
    gather); ``evict_id`` drops the render state of one id and ``evict_dir`` of everything
    under a directory.
    """

    def __init__(
        self,
        storage: Callable[[], Storage],
        *,
        held_by: Callable[[frozenset[str]], Awaitable[list[ReferenceHolder]]],
        evict_id: Callable[[str], None],
        evict_dir: Callable[[str], None],
    ) -> None:
        """Write through the provider ``storage`` returns, checking deletes against ``held_by``."""
        self._storage = storage
        self._held_by = held_by
        self._evict_id = evict_id
        self._evict_dir = evict_dir

    async def _refuse_held(self, ids: frozenset[str], head: str) -> None:
        """Raise :class:`StoredObjectHeldError` naming every holder of one of ``ids``; nothing held passes.

        A referee's own ``OperationError`` propagates as it is. Any other fault is logged and
        raised as :class:`HolderUnreadableError`, whose text carries only the fault's type.
        """
        try:
            holders = await self._held_by(ids)
        except OperationError:
            raise
        except Exception as exc:
            logger.exception("%s: a holder could not be read", head)
            raise HolderUnreadableError(head, type(exc).__name__) from exc
        if holders:
            raise StoredObjectHeldError(head, holders)

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
        A held object raises :class:`StoredObjectHeldError` naming its holders and deletes
        nothing; a holder that cannot be read raises :class:`HolderUnreadableError`; a missing
        object raises ``FileNotFoundError``.
        """
        await self._refuse_held(frozenset({path}), head=f"stored template '{path}' cannot be deleted")
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
        Refused with :class:`StoredObjectHeldError`, deleting and evicting nothing, when a stored
        object names any object under ``path/``. Eviction runs even on a partial-delete failure.
        A failure once the provider's deletion may have begun is raised as
        :class:`PartialDirectoryDeleteError` with the provider's failure as its cause; the
        provider's own pre-deletion ``FileNotFoundError``/``ValueError`` propagate as they are.
        """
        assert_not_root(path)
        storage = self._storage()
        ids = frozenset(await storage.list(path))
        await self._refuse_held(ids, head=f"directory '{path}' cannot be deleted")
        try:
            await storage.delete_dir(path)
        except (FileNotFoundError, ValueError):
            # The contract's pre-deletion checks (a missing directory, a rejected path):
            # Storage.delete_dir raises them before any object is deleted.
            raise
        except Exception as exc:
            raise PartialDirectoryDeleteError(path, exc) from exc
        finally:
            # Dir-delete is non-atomic: a failure can still leave some files
            # deleted, so evict regardless of outcome — never serve content under
            # this dir after a partial delete. Only keys under the prefix are
            # dropped; templates outside it stay cached.
            self._evict_dir(path)
