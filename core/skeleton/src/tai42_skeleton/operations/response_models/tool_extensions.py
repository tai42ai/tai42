"""Response models for the tool-extensions operations: a tool's applied combos and the extension catalog."""

from __future__ import annotations

from pydantic import BaseModel
from tai42_contract.manifest import ExtensionElement

from tai42_skeleton.operations.response_models.extensions import ExtensionView


class ToolExtensionsView(BaseModel):
    """A tool's applied extension combos plus the catalog of available extensions.

    Each ``available`` entry is one registered extension's catalog row.
    """

    combos: list[list[ExtensionElement]]
    available: list[ExtensionView]


class ToolExtensionCombosModified(ToolExtensionsView):
    """A combo edit's reply: the tool's new combos and the extension catalog, plus the write's span release.

    ``span_release_error`` is ``null`` when the config store released its exclusive write span after the persist;
    otherwise the store's error — the change landed and its reload and broadcast still ran, but the store may still
    hold its write span until the writer process exits, so a later write can block or be refused until then.
    """

    span_release_error: str | None
