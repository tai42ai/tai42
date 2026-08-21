"""A fixture tool that returns the async-park sentinel, for the dispatch-path
preservation of a ``SuspendedInteraction`` through a preset (``TransformedTool``)."""

from tai42_contract.app import tai42_app
from tai42_contract.interactions import SuspendedInteraction


@tai42_app.tools.tool
def make_suspend() -> SuspendedInteraction:
    """Return an async-park sentinel."""
    return SuspendedInteraction(interaction_id="i-preset")
