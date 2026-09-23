"""The composed Postgres store over the subject-keyed record substrate's tables."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .attachments import _AttachmentStore
from .connection import _StoreConnection
from .declarations import _DeclarationStore
from .queries import _RecordQueryStore
from .records import _RecordReadStore
from .restore import _RestoreStore
from .retention import _RetentionStore
from .templates import _TemplateStore
from .writes import _RecordWriteStore


class PostgresStatesStore(
    _StoreConnection,
    _DeclarationStore,
    _TemplateStore,
    _AttachmentStore,
    _RecordReadStore,
    _RecordWriteStore,
    _RecordQueryStore,
    _RestoreStore,
    _RetentionStore,
):
    """One class over the record substrate's tables, composed from the per-table concern mixins.

    Its only instance state is the version-gated attachments-composition cache the write
    path consults; each method otherwise opens its own pooled connection, and a
    multi-statement operation runs in one explicit transaction. A caller that must span
    several writes atomically opens :meth:`begin` and threads the yielded connection into
    the write methods' ``conn`` parameter — they join that transaction instead of opening
    their own. A mixin method reaches a sibling table's method through the composed instance.
    """

    def __init__(self) -> None:
        """Bind the per-process attachments-composition cache (bounded, version-gated)."""
        self._attachment_paths_cache: OrderedDict[tuple[str, Any], tuple[Any, Any]] = OrderedDict()
