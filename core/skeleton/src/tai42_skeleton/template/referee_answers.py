"""The check every referee answer passes before a gather uses it.

A referee is a plugin's or the platform's callable a gather asks for holders. An answer of the
wrong shape is the referee's bug: it raises ``TypeError`` naming the referee, never a silent skip.
"""

from __future__ import annotations

from typing import TypeGuard

from tai42_contract.tools import ReferenceHolder


def referee_name(referee: object) -> str:
    """The dotted ``module.qualname`` a referee's error names it by."""
    module = getattr(referee, "__module__", None) or type(referee).__module__
    qualname = getattr(referee, "__qualname__", None) or type(referee).__qualname__
    return f"{module}.{qualname}"


def is_holder_list(answer: object) -> TypeGuard[list[ReferenceHolder]]:
    """Whether ``answer`` is a list of ``ReferenceHolder``, the shape a referee answers holders in."""
    return isinstance(answer, list) and all(isinstance(entry, ReferenceHolder) for entry in answer)


def checked_holders(referee: object, answer: object) -> list[ReferenceHolder]:
    """``answer`` when it is a list of ``ReferenceHolder``; any other answer raises ``TypeError`` naming ``referee``."""
    if not is_holder_list(answer):
        raise TypeError(
            f"referee {referee_name(referee)} answered {answer!r}; a referee answers a list of ReferenceHolder"
        )
    return answer


__all__ = ["checked_holders", "is_holder_list", "referee_name"]
