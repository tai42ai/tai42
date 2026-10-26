"""Response models for the sandbox operations."""

from __future__ import annotations

from pydantic import BaseModel


class SandboxPolicy(BaseModel):
    """The resolved sandbox policy surfaced to external consumers.

    The network ``egress`` ceiling, the ``isolation`` floor, the
    ``scrub_transcript`` flag and the ``durable`` gate. Present regardless of
    whether a provider is registered.
    """

    egress: str
    isolation: str
    scrub_transcript: bool
    durable: bool


class SandboxInfo(BaseModel):
    """Sandbox identity plus the always-present resolved ``policy``.

    ``provider``/``module`` are ``null`` and ``sessions`` is 0 when no provider is
    registered (``present`` false).
    """

    present: bool
    provider: str | None = None
    module: str | None = None
    sessions: int
    policy: SandboxPolicy
