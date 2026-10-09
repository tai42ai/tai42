"""Keys a registrant sets in a tool's ``meta`` and the platform reads."""

from __future__ import annotations

from typing import Final

from tai42_contract.errors import ErrorKind

TOOL_META_PAUSES: Final = "tai42/pauses"
"""Value ``True``: this tool can pause — it can return a park signal (``SuspendedInteraction`` or
``ResumeBuffered``) because it parks, asks, or waits on a participant out of band. A tool without it never pauses."""


class UndeclaredPauseError(RuntimeError):
    """A tool that does not declare ``TOOL_META_PAUSES`` returned a park signal.

    Stamped ``unknown``: no member of the taxonomy names a registration fault, and ``unknown``
    is never retryable, so no retry policy re-fires the tool body whose park already happened.
    """

    __tai_error_kind__ = ErrorKind.UNKNOWN

    def __init__(self, tool: str) -> None:
        """Name the tool that returned the park signal."""
        self.tool = tool
        super().__init__(
            f"tool {tool!r} returned a park signal but does not declare that it can pause; "
            f"register it with meta={{{TOOL_META_PAUSES!r}: True}}"
        )


__all__ = ["TOOL_META_PAUSES", "UndeclaredPauseError"]
