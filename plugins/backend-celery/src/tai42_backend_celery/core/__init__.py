"""Core Celery backend package.

Imported by the top-level package for their registration side effects:
``core.backend`` registers :class:`CeleryBackend`, ``core.app`` builds the Celery
application and installs the pool-child fork-safety hooks, and ``core.tasks``
registers the Celery task surface.
"""
