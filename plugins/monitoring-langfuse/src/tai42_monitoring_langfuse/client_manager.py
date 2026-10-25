"""Owns the one HTTP client the reader queries Langfuse's public REST API through.

The client carries the project's base URL, its key pair as HTTP Basic auth, and its timeout. Records are written
by the kit's OpenTelemetry writer and reach Langfuse through the deployment's collector.
"""

from __future__ import annotations

import threading

import httpx

from tai42_monitoring_langfuse.project import LangfuseProject


class LangfuseClientManager:
    """Builds the project's read client lazily, once."""

    def __init__(self, project: LangfuseProject) -> None:
        """Bind the Langfuse ``project`` this manager reads from."""
        self._project = project
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def active_client(self) -> httpx.Client:
        """The project's HTTP client, built on first use."""
        client = self._client
        if client is not None:
            return client
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(
                    base_url=self._project.host,
                    auth=httpx.BasicAuth(self._project.public_key, self._project.secret_key),
                    timeout=self._project.timeout_seconds,
                )
            return self._client

    def active_source(self) -> str:
        """The project's ``source`` (the Langfuse ``environment``) every read is scoped to."""
        return self._project.source
