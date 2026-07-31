"""Backend tool extensions.

Imported by the top-level ``tai42_backend_celery`` package: the import registers
the ``sync_task`` / ``schedule_task`` / ``async_task`` extensions through
``tai42_app.extensions.extension`` (BACKEND kind); each branches a tool into a
variant that executes through the Celery backend instead of in-process.
"""
