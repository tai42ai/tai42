"""The one guard every agent face of this plugin runs over its keyword arguments.

A face declares every run keyword it reads as a named parameter and collects the rest in a
trailing ``**unknown``. The contract's ``Agent.run`` names four keys the platform supplies or
reads; every other keyword is the agent's own, so a keyword no face declares is a caller error
refused loudly, never dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from tai42_agents._internal.reject import reject_unhonored

#: The keys the contract's ``Agent.run`` names: the platform supplies or reads them.
CONTRACT_RUN_KEYS: Final[frozenset[str]] = frozenset({"thread_id", "user_message", "tool_names", "response_format"})

_EMPTY: Final[Mapping[str, str]] = {}


def guard_run_kwargs(
    face: str,
    unknown: Mapping[str, Any],
    contract_values: Mapping[str, Any],
    unhonored: Mapping[str, str] = _EMPTY,
) -> None:
    """Refuse an undeclared keyword, then any contract key this face cannot honor that the caller set.

    ``face`` is the ``agent.method`` label opening the message; ``unknown`` is the face's trailing
    ``**unknown``; ``contract_values`` maps the contract keys to the values the caller passed;
    ``unhonored`` maps each contract key this face cannot honor to the reason the refusal names.
    """
    if unknown:
        raise TypeError(f"{face} got unknown run keyword argument(s): {', '.join(sorted(unknown))}")
    reject_unhonored(face, contract_values, unhonored, collection_params=frozenset({"tool_names"}))
