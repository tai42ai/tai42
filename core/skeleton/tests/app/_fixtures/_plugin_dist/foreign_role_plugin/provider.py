"""The non-route lifecycle leaf: records a marker at import, mounts no route.

Reached under NO mount binding during the foreign-role package walk (its role never
mounts a route), so it must import cleanly without touching ``mount_base()``.
"""

from __future__ import annotations

REGISTERED = False


def _register() -> None:
    global REGISTERED
    REGISTERED = True


_register()
