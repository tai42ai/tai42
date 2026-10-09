"""The signal the template store fires after it drops cached template content."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateEviction:
    """Stored template content dropped from this process's template caches.

    ``path`` is a template id, a directory key when ``prefix``, or ``None`` for every template.
    """

    path: str | None
    prefix: bool = False

    def covers(self, template_id: str) -> bool:
        """Whether this eviction drops ``template_id``."""
        if self.path is None:
            return True
        if self.prefix:
            return template_id.startswith(self.path.rstrip("/") + "/")
        return template_id == self.path


__all__ = ["TemplateEviction"]
