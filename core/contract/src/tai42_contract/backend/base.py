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
    """

    @abstractmethod
    async def launch(self, args: Sequence[str]) -> None:
        """Start the worker runtime for the backend registered via
        ``@tai42_app.backends.register_backend``."""
