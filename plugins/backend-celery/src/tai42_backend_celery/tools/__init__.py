"""The ``backend_*`` tool surface.

Imported by the top-level ``tai42_backend_celery`` package: the import registers
the task/worker introspection tools and the RedBeat schedule tools through
``tai42_app.tools.tool``. Four of them are the host's scheduling marker tools
(``backend_list_schedules`` / ``backend_delete_schedule`` /
``backend_export_schedules`` / ``backend_import_schedules``) — their presence
is how the host detects that a scheduling-capable backend is installed.
"""
