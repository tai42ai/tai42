"""Template ids storage answered "not found" for, remembered so a render does not re-read them."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class AbsentTemplateIds:
    """A bounded, expiring set of template ids storage answered "not found" for.

    Each id lives ``ttl`` seconds (no expiry when ``None``); past ``max_size`` (unbounded when
    ``None``) the oldest is dropped. ``enabled`` False remembers nothing. Locked: the include
    loader reads and writes it on render threads.
    """

    def __init__(self, *, ttl: int | None, max_size: int | None, enabled: bool) -> None:
        """Start empty with the given bounds."""
        self._ids: OrderedDict[str, float | None] = OrderedDict()
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max_size = max_size
        self._enabled = enabled

    def __contains__(self, template_id: object) -> bool:
        """Whether ``template_id`` is remembered absent and not yet expired."""
        if not self._enabled or not isinstance(template_id, str):
            return False
        with self._lock:
            if template_id not in self._ids:
                return False
            expiry = self._ids[template_id]
            if expiry is not None and expiry <= time.monotonic():
                del self._ids[template_id]
                return False
            return True

    def add(self, template_id: str) -> None:
        """Remember ``template_id`` as absent (the oldest dropped past the bound)."""
        if not self._enabled:
            return
        expiry = None if self._ttl is None else time.monotonic() + self._ttl
        with self._lock:
            self._ids[template_id] = expiry
            self._ids.move_to_end(template_id)
            while self._max_size is not None and len(self._ids) > self._max_size:
                self._ids.popitem(last=False)

    def forget(self, matches: Callable[[str], bool]) -> None:
        """Forget every remembered id that ``matches``."""
        with self._lock:
            for template_id in [tracked for tracked in self._ids if matches(tracked)]:
                del self._ids[template_id]
