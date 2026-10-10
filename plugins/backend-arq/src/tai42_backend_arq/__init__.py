"""arq execution backend for the TAI ecosystem.

Importing this package registers everything on the global ``tai42_app`` handle as
a side-effect: the :class:`ArqBackend`, the ``backend_*`` tool surface, the
``sync_task`` / ``schedule_task`` / ``async_task`` BACKEND extensions. The host
names this package in its manifest's ``backend_module`` field and imports it at
startup.
"""

from tai42_backend_arq import extensions, tools
from tai42_backend_arq.backend import ArqBackend
from tai42_backend_arq.settings import TaskFailedError

__all__ = ["ArqBackend", "TaskFailedError", "extensions", "tools"]
