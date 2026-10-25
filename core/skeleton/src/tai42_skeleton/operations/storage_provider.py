"""The registered storage provider, as every storage door reads it."""

from __future__ import annotations

from tai42_contract.storage import Storage

from tai42_skeleton.app import instance
from tai42_skeleton.operations import NotSupportedError

NO_PROVIDER_MESSAGE = "storage needs a storage-provider plugin"


def storage_provider() -> Storage | None:
    """The registered storage provider, or ``None`` while dead by default."""
    return instance.app.storage.provider


def require_storage_provider() -> Storage:
    """The registered provider, or a loud ``501`` when none is installed."""
    provider = storage_provider()
    if provider is None:
        raise NotSupportedError(NO_PROVIDER_MESSAGE)
    return provider
