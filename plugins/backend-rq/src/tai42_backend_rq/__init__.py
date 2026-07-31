"""RQ execution backend for the TAI ecosystem.

Importing this package registers everything on the global ``tai42_app`` handle as
a side-effect (how the host discovers the backend named by the manifest's
``backend_module``): :class:`RqBackend`, the ``backend_*`` tool surface, and the
``sync_task`` / ``schedule_task`` / ``async_task`` BACKEND extensions.
"""

import tai42_backend_rq.extensions
import tai42_backend_rq.tools  # noqa: F401  (import-time tool registration)
from tai42_backend_rq.backend import RqBackend

__all__ = ["RqBackend"]
