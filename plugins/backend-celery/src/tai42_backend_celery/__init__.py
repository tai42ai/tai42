"""Celery execution backend for the TAI ecosystem.

Importing this package registers everything on the global ``tai42_app`` handle as
a side-effect: the :class:`CeleryBackend`, the Celery application with its
pool-child fork-safety hooks, the Celery task surface, the ``backend_*`` tool
surface, and the ``sync_task`` / ``schedule_task`` / ``async_task`` BACKEND
extensions. The host names this package in its manifest's ``backend_module``
field and imports it at startup.
"""

import tai42_backend_celery.core.tasks  # registers the Celery task surface
import tai42_backend_celery.extensions.extensions  # registers the tool extensions
import tai42_backend_celery.tools.tools  # noqa: F401  (registers the backend_* tools)
from tai42_backend_celery.core.backend import CeleryBackend

__all__ = ["CeleryBackend"]
