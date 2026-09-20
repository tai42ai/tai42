"""The shared facet base binding the app handle."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_skeleton.app.server import TaiMCP


class _Facet:
    """Common base: binds the facet to its owning app."""

    __slots__ = ("_app",)

    def __init__(self, app: TaiMCP) -> None:
        self._app = app
