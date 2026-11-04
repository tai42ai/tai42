"""Celery execution backend for the TAI ecosystem.

Importing this package registers everything on the global ``tai42_app`` handle as
a side-effect: the :class:`CeleryBackend`, the Celery application, the Celery task
surface, the ``sync_task`` / ``schedule_task`` / ``async_task`` BACKEND extensions
(the kit's branch tools over :class:`CeleryTaskDoor` and :class:`CeleryScheduleStore`),
and the kit's ``backend_*`` tools over the same store and :class:`CeleryInspection`.
The import connects no receiver to Celery's signals and installs no fork-safety hook:
the runtime that runs the application does both, as it builds and runs. The host names
this package in its manifest's ``backend_module`` field and imports it at startup.
"""

from tai42_kit.backend import register_inspection_tools, register_schedule_tools, register_task_branches

import tai42_backend_celery.core.tasks  # noqa: F401  (registers the Celery task surface)
from tai42_backend_celery.core.backend import CeleryBackend
from tai42_backend_celery.core.inspection import CeleryInspection
from tai42_backend_celery.core.schedule_store import CeleryScheduleStore
from tai42_backend_celery.core.settings import celery_settings
from tai42_backend_celery.core.task_door import CeleryTaskDoor

_store = CeleryScheduleStore()
register_task_branches(CeleryTaskDoor(), _store, celery_settings, CeleryBackend.label)
register_schedule_tools(_store, celery_settings, CeleryBackend.label, __name__)
register_inspection_tools(CeleryInspection(CeleryBackend.label), __name__)

__all__ = ["CeleryBackend"]
