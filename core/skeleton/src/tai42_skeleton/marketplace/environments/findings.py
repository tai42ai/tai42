"""How an inconsistency of the plugins directory is worded, once for every reader.

A :class:`Finding` reads the same for ``tai doctor``, for ``tai plugins rebuild`` and as a
refusal to start. The fix it names is one of two tails: :data:`REBUILD_DEPLOYMENT` when the
directory does not match the deployment's install store or holds a build that does not read,
:data:`REBUILD_IMAGE` when it does not suit the running image.
"""

from __future__ import annotations

from dataclasses import dataclass

REBUILD_DEPLOYMENT = ". Run 'tai plugins rebuild' against this deployment, then start again"
REBUILD_IMAGE = ". Run 'tai plugins rebuild' against this image"

#: How the plugin layer is named in a finding.
LAYER_SUBJECT = "plugin environment"


@dataclass(frozen=True)
class Finding:
    """One inconsistency, worded once: ``head``, an optional ``detail``, and the fix in ``tail``."""

    head: str
    detail: str | None = None
    tail: str = ""

    def boot_text(self) -> str:
        """The finding as a refusal to start."""
        return self.head + " — refusing to start" + (f": {self.detail}" if self.detail else "") + self.tail

    def report_text(self) -> str:
        """The finding as a check reports it."""
        return self.head + (f": {self.detail}" if self.detail else "") + self.tail
