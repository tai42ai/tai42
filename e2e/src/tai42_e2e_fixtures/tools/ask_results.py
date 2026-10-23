"""The two shapes an ``ask`` returns, narrowed for the probes that call it.

A sync ``ask`` answers with an ``AskAnswer`` and an async one parks and returns a
``SuspendedInteraction``; the helper's declared return is their union. A probe that asked in
one mode reads the matching shape through ``answered`` or ``parked``, and any other value
raises ``TypeError``.
"""

from __future__ import annotations

from tai42_contract.interactions import AskAnswer, SuspendedInteraction


def answered(result: AskAnswer | SuspendedInteraction) -> AskAnswer:
    """The ``AskAnswer`` a sync ask returned; raises ``TypeError`` for any other value."""
    if not isinstance(result, AskAnswer):
        raise TypeError(f"a sync ask answered with {type(result).__name__}, not an AskAnswer")
    return result


def parked(result: AskAnswer | SuspendedInteraction) -> SuspendedInteraction:
    """The ``SuspendedInteraction`` an async ask returned; raises ``TypeError`` for any other value."""
    if not isinstance(result, SuspendedInteraction):
        raise TypeError(f"an async ask returned {type(result).__name__}, not a SuspendedInteraction")
    return result
