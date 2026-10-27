"""Response models for the backend operations."""

from __future__ import annotations

from pydantic import BaseModel


class BackendInfo(BaseModel):
    """Backend identity.

    ``backend``/``module`` are ``null`` when no provider is registered
    (``present`` false).
    """

    present: bool
    backend: str | None = None
    module: str | None = None
