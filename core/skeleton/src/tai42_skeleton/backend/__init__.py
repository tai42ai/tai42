"""Backend feature: the execution-backend registration seam.

The :class:`~tai42_contract.backend.Backend` ABC is the contract; concrete worker
backends (celery/rq/arq) are external plugins that implement it and register via
``@tai42_app.backends.register_backend``. This package re-exports the ABC as the
registration seam.
"""

from tai42_contract.backend import Backend

__all__ = [
    "Backend",
]
