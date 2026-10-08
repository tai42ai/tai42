"""Owns the one Langfuse API client the reader queries through.

The client is built with ``tracing_enabled=False``: the Langfuse SDK builds no tracer
provider and no span processor, only the API clients. Records are written by the kit's
OpenTelemetry writer and reach Langfuse through the deployment's collector.
"""

from __future__ import annotations

import threading

from langfuse import Langfuse

from tai42_monitoring_langfuse.project import LangfuseProject


class LangfuseClientManager:
    """Builds the project's read client lazily, once."""

    def __init__(self, project: LangfuseProject) -> None:
        """Bind the Langfuse ``project`` this manager reads from."""
        self._project = project
        self._client: Langfuse | None = None
        self._lock = threading.Lock()

    def active_client(self) -> Langfuse:
        """The project's API client, built on first use."""
        client = self._client
        if client is not None:
            return client
        with self._lock:
            if self._client is None:
                self._client = Langfuse(
                    public_key=self._project.public_key,
                    secret_key=self._project.secret_key,
                    base_url=self._project.host,
                    timeout=self._project.timeout_seconds,
                    environment=self._project.source,
                    tracing_enabled=False,
                )
            return self._client

    def active_source(self) -> str:
        """The project's ``source`` (the Langfuse ``environment``) every read is scoped to."""
        return self._project.source

    def read_timeout_seconds(self) -> int:
        """Read timeout; the generated API client does not inherit the SDK client timeout."""
        return self._project.timeout_seconds
