"""The conversation-target kind — a top-level leaf carrying only the ``Literal``.

A module that needs the type (the states models, whose subject scope is a conversation
target) imports it from here. It cannot live under the ``conversations`` package: a
submodule import runs that package's ``__init__``, which pulls the channels + interactions
models, and the states models are imported by ``interactions.models`` (the park carrier) —
routing the alias through the package would close that import cycle. This leaf imports
only ``typing``.
"""

from __future__ import annotations

from typing import Literal

#: A conversation route's target: an ``agent`` run or a ``tool`` dispatch. This pair is
#: also the state store's subject scope (the only tenancy this platform has).
ConversationTargetKind = Literal["agent", "tool"]
