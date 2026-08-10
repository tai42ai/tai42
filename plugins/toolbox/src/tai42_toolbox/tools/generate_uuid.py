"""The ``generate_uuid`` tool: generate a random UUID.

No heavy backing dependency, so this module imports cleanly in the base install.
"""

from __future__ import annotations

import uuid

from tai42_contract.app import tai42_app


@tai42_app.tools.tool(tags={"uuid"})
def generate_uuid() -> str:
    """Generate a random UUID (version 4)."""
    return str(uuid.uuid4())
