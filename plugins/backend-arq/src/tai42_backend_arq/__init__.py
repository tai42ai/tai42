"""arq execution backend for the TAI ecosystem.

Importing this package registers everything on the global ``tai42_app`` handle as
a side-effect: the :class:`ArqBackend`, the ``sync_task`` / ``schedule_task`` /
``async_task`` BACKEND extensions (the kit's branch tools over :class:`ArqTaskDoor`
and :class:`ArqScheduleStore`), and the kit's ``backend_*`` tool surface over the
same store and :class:`ArqInspection`. The host names this package in its
manifest's ``backend_module`` field and imports it at startup.
"""

from tai42_kit.backend import register_inspection_tools, register_schedule_tools, register_task_branches

from tai42_backend_arq.backend import ArqBackend
from tai42_backend_arq.inspection import ArqInspection
from tai42_backend_arq.schedule_store import ArqScheduleStore
from tai42_backend_arq.settings import TaskFailedError, arq_settings
from tai42_backend_arq.task_door import ArqTaskDoor

_store = ArqScheduleStore()
register_task_branches(ArqTaskDoor(), _store, arq_settings, ArqBackend.label)
register_schedule_tools(_store, arq_settings, ArqBackend.label, __name__)
register_inspection_tools(ArqInspection(ArqBackend.label), __name__)

__all__ = ["ArqBackend", "TaskFailedError"]
