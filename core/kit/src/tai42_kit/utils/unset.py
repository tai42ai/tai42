"""The omitted-value marker of a synthesized signature, and the helper that drops it.

:data:`UNSET` is the default of every optional parameter of a synthesized signature that a
caller may leave out (an agent run tool's per-field signature, the typed runner of a branch over
a preset whose input is an authored schema). The bodies that receive it drop it, and a wrapper
that records or ships the caller's kwargs passes them through :func:`without_unset`, so no tool
body, job payload, worker or monitoring record sees it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic_core import core_schema


class Unset:
    """The type of :data:`UNSET`, the value of a parameter the caller did not supply.

    ``__get_pydantic_core_schema__`` maps it to an ``any`` schema, so pydantic derives a parameter
    defaulting to it as optional and omits the non-serializable default from the JSON schema.
    """

    __get_pydantic_core_schema__ = classmethod(lambda cls, source, handler: core_schema.any_schema())


UNSET = Unset()


def without_unset(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Return a new dict of every entry of ``kwargs`` whose value is not :data:`UNSET`; ``None`` is kept."""
    return {key: value for key, value in kwargs.items() if value is not UNSET}
