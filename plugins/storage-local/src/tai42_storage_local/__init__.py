"""Local-filesystem ``Storage`` backend for the TAI ecosystem.

Importing this package registers :class:`~tai42_storage_local.storage.LocalStorage`
as the active storage provider via an import side-effect.
"""

from __future__ import annotations

from tai42_storage_local.driver import AsyncLocalDriver
from tai42_storage_local.settings import LocalStorageSettings, storage_settings
from tai42_storage_local.storage import LocalStorage

__all__ = [
    "AsyncLocalDriver",
    "LocalStorage",
    "LocalStorageSettings",
    "storage_settings",
]
