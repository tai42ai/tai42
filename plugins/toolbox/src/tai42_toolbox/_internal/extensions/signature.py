"""Signature helpers shared by the tool extensions."""

from __future__ import annotations

import inspect


def with_added_params(sig: inspect.Signature, *new_params: inspect.Parameter) -> inspect.Signature:
    """Return ``sig`` with ``new_params`` inserted before any trailing ``**kwargs``.

    A parameter after a ``VAR_KEYWORD`` is an invalid signature order, so the new ones
    go just ahead of it (or at the end when the tool has no ``**kwargs``)."""
    params = list(sig.parameters.values())
    index = len(params)
    for position, existing in enumerate(params):
        if existing.kind is inspect.Parameter.VAR_KEYWORD:
            index = position
            break
    for offset, param in enumerate(new_params):
        params.insert(index + offset, param)
    return sig.replace(parameters=params)
