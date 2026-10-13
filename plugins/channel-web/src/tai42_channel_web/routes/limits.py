"""The flood-control families and budgets the web channel's public doors declare.

Declared on the routes, so the budgets hold whatever mount base the plugin is installed under; an
operator's ``TAI_RATE_LIMIT_FAMILIES__<FAMILY>__*`` setting still overrides either field.
"""

from __future__ import annotations

from typing import Final

from tai42_contract.app import RateLimitBudget

# The chat family is wider than the app-wide default's burst would need for one send: ONE first
# page load is the page plus every bundle file it links, all in one burst.
CHAT_RATE_LIMIT_FAMILY: Final = "channels_web"
CHAT_RATE_LIMIT_BUDGET: Final = RateLimitBudget(limit=120, burst=30)

# The upload family is far tighter than the chat family: an upload request carries up to the
# per-kind media cap, so its cost class is far above a text message, and a text-sized budget would
# admit a large byte volume.
UPLOAD_RATE_LIMIT_FAMILY: Final = "channels_web_uploads"
UPLOAD_RATE_LIMIT_BUDGET: Final = RateLimitBudget(limit=20, burst=5)
