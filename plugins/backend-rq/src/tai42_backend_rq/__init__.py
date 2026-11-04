"""RQ execution backend for the TAI ecosystem.

Importing this package registers everything on the global ``tai42_app`` handle as
a side-effect (how the host discovers the backend named by the manifest's
``backend_module``): :class:`RqBackend`, the ``sync_task`` / ``schedule_task`` /
``async_task`` BACKEND extensions (the kit's branch tools over :class:`RqTaskDoor`
and :class:`RqScheduleStore`), and the kit's ``backend_*`` tool surface over the
same store and :class:`RqInspection`.
"""

from tai42_kit.backend import register_inspection_tools, register_schedule_tools, register_task_branches

from tai42_backend_rq.backend import RqBackend
from tai42_backend_rq.inspection import RqInspection
from tai42_backend_rq.schedule_store import RqScheduleStore
from tai42_backend_rq.settings import rq_settings
from tai42_backend_rq.task_door import RqTaskDoor

_store = RqScheduleStore()
register_task_branches(RqTaskDoor(), _store, rq_settings, RqBackend.label)
register_schedule_tools(_store, rq_settings, RqBackend.label, __name__)
register_inspection_tools(RqInspection(RqBackend.label), __name__)

__all__ = ["RqBackend"]
