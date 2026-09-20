"""The ambient state-context seam as the skeleton's doors and facet see it.

The carrier itself lives in the kit (:mod:`tai42_kit.utils.state_context`) so the
execution backends below the skeleton can deposit it without importing tai42-skeleton;
this module re-exports it under the skeleton's package so a door and the ``tai42_app.states``
facet reach one seam. A door wraps the work it drives in :func:`state_context`; the write
chokepoint and the facet's ``context()`` read it back through :func:`current_state_context`.
"""

from __future__ import annotations

from tai42_kit.utils.state_context import current_state_context, state_context

__all__ = ["current_state_context", "state_context"]
