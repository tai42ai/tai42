"""Plain tools whose parameters hold ``TemplatedText`` at different depths, and one typed ``Any``."""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText


@tai42_app.tools.tool
def render_items(items: list[TemplatedText], sep: str = "\n") -> str:
    """Render each item and join them."""
    return sep


@tai42_app.tools.tool
def shaped_answer(question: str, response_format: TemplatedText | dict[str, Any] | None = None) -> str:
    """Answer under a forced output shape."""
    return question


@tai42_app.tools.tool
def opaque_payload(payload: Any = None) -> str:
    """Take an opaque payload."""
    return "ok"
