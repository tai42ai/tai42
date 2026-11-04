"""Abstract execution-backend interface a Tai app's task runtime implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence


class Backend(ABC):
    """Abstract execution backend for a Tai app.

    A backend runs the task runtime (``launch``) and executes the work its
    workers pull from the broker. The app core depends only on this interface
    and stays backend-agnostic; concrete backends (celery/rq/arq) implement it
    and register via ``@tai42_app.backends.register_backend``.

    Fleet propagation of config changes is not a backend concern: it is the
    app's own worker bus, internal to the app. A backend-runtime process
    receives fleet ops through the app's bus subscription exactly like a serving
    HTTP worker — the backend carries no control-plane surface of its own.

    Each backend declares its own drain bound (:attr:`drain_timeout`); the app
    announces it, together with its own shutdown bounds, when the process leaves,
    so a fleet restart waits for the worker to finish its in-flight work rather
    than reporting it timed out.
    """

    @property
    @abstractmethod
    def drain_timeout(self) -> float:
        """How long, in seconds, this backend's worker may take to finish its in-flight work and exit when stopped.

        Read at call time, so a settings change applies to the next stop.
        """

    @abstractmethod
    async def launch(self, args: Sequence[str]) -> None:
        """Start the worker runtime for the backend registered via ``@tai42_app.backends.register_backend``."""
