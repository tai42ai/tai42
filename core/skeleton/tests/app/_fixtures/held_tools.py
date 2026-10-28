"""Fixture tool module: a tool whose reply the test releases.

``held_reply`` sets ``ENTERED`` once its body runs and answers only after the test sets
``RELEASE``, so a test can act while the call is in flight. A boot re-imports this module,
so a test reads both events from ``sys.modules`` after the served app has booted.
"""

import asyncio

from tai42_contract.app import tai42_app

ENTERED = asyncio.Event()
RELEASE = asyncio.Event()


@tai42_app.tools.tool
async def held_reply(text: str) -> str:
    """Answer ``text`` once the test releases the call."""
    ENTERED.set()
    await RELEASE.wait()
    return text
