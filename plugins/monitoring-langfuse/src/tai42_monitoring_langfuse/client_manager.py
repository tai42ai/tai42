"""Owns the per-project Langfuse clients and the scope / disable context.

``scope(public_key)`` selects the active project for a block, and every emit
resolves the active client explicitly so a multi-project process never falls into
Langfuse's disabled-on-ambiguity path.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from langfuse import Langfuse, get_client
from tai42_contract.monitoring import ProjectConfig

from tai42_monitoring_langfuse.sdk_internals import (
    bind_public_key,
    current_scoped_public_key,
    evict_all_clients,
)

logger = logging.getLogger(__name__)

# Suppression flag for ``disable()``. Separate from Langfuse's own machinery so
# every emit path honors it uniformly.
_disabled: ContextVar[bool] = ContextVar("langfuse_monitoring_disabled", default=False)

# Read timeout used when the active project is not one this manager configured
# (e.g. another in-process component bound the scope to its own project).
_DEFAULT_READ_TIMEOUT_SECONDS = 30


class LangfuseClientManager:
    """Per-project client cache plus the scope / disable contextvars."""

    def __init__(
        self,
        projects: list[ProjectConfig],
        default_public_key: str,
    ) -> None:
        if not projects:
            raise ValueError("LangfuseClientManager requires at least one project")
        self._projects: dict[str, ProjectConfig] = {p.public_key: p for p in projects}
        if default_public_key not in self._projects:
            raise ValueError(f"default_public_key {default_public_key!r} is not among the configured projects")
        self._default_public_key = default_public_key
        # Built clients, cached per public_key so emits don't reconstruct a
        # wrapper (and take Langfuse's global lock) on every call.
        self._clients: dict[str, Langfuse] = {}
        self._built = False
        # Guards _projects / _clients / _built — add_project and the reader's
        # worker threads run concurrently with emit-path reads.
        self._lock = threading.RLock()

    def _build_client(self, cfg: ProjectConfig) -> Langfuse:
        return Langfuse(
            public_key=cfg.public_key,
            secret_key=cfg.secret_key,
            host=cfg.host,
            timeout=cfg.timeout_seconds,
            environment=cfg.source,
        )

    def _ensure_built(self) -> None:
        """Construct and cache a Langfuse client per project once; rebuilds after shutdown."""
        if self._built:
            return
        with self._lock:
            if self._built:
                return
            for cfg in self._projects.values():
                self._clients[cfg.public_key] = self._build_client(cfg)
            self._built = True

    def add_project(self, config: ProjectConfig) -> None:
        """Register an additional project after construction; idempotent.

        If the clients are already built, the new one is constructed immediately
        so ``scope()`` can select it.
        """
        with self._lock:
            if config.public_key in self._projects:
                return
            self._projects[config.public_key] = config
            if self._built:
                self._clients[config.public_key] = self._build_client(config)

    def resolve_public_key(self) -> str:
        """The active project: the scoped key if one is bound, else the default."""
        return current_scoped_public_key() or self._default_public_key

    def active_client(self) -> Langfuse:
        self._ensure_built()
        public_key = self.resolve_public_key()
        client = self._clients.get(public_key)
        if client is not None:
            return client
        # Active project not configured here (another component scoped it);
        # resolve via the shared registry rather than failing.
        return get_client(public_key=public_key)

    def active_source(self) -> str:
        """The ``source`` (Langfuse ``environment``) of the active project.

        Every read scopes to this so a shared project returns only our data.
        Raises if the active key has no registered config, never silently
        reading unscoped.
        """
        public_key = self.resolve_public_key()
        cfg = self._projects.get(public_key)
        if cfg is None:
            raise ValueError(f"active project {public_key!r} has no registered config; cannot resolve its source")
        return cfg.source

    def read_timeout_seconds(self) -> int:
        """Read timeout for the active project. The generated API client does not
        inherit the SDK client timeout, so reads must pass it explicitly."""
        cfg = self._projects.get(self.resolve_public_key())
        return cfg.timeout_seconds if cfg else _DEFAULT_READ_TIMEOUT_SECONDS

    def is_disabled(self) -> bool:
        return _disabled.get()

    @contextmanager
    def scope(self, public_key: str) -> Iterator[None]:
        """Bind the active project for the block; raises on an unconfigured key."""
        if public_key not in self._projects:
            raise ValueError(f"scope({public_key!r}): no Langfuse project with that public_key is configured")
        self._ensure_built()
        with bind_public_key(public_key):
            yield

    @contextmanager
    def disable(self) -> Iterator[None]:
        token = _disabled.set(True)
        try:
            yield
        finally:
            _disabled.reset(token)

    def flush(self) -> None:
        # Nothing is buffered before the first client is built, so don't build just to flush.
        if not self._built:
            return
        for client in list(self._clients.values()):
            client.flush()

    def shutdown(self) -> None:
        """Fully evict every cached client so the next use rebuilds clean.

        A bare ``client.shutdown()`` only flushes, leaving the dead instance in
        the singleton; ``evict_all_clients`` clears every project.
        """
        evict_all_clients()
        with self._lock:
            self._clients.clear()
            self._built = False
