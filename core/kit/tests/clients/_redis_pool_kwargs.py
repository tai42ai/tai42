"""Comparing the connection kwargs of two Redis pools built from the same arguments."""

from typing import Any


def comparable_connection_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """A pool's connection kwargs with each value that has no equality of its own as its type.

    redis-py builds some of these values per pool (a registry, a handler); two pools built
    from the same arguments hold equal kwargs once those values are compared by type.
    """
    return {key: type(value) if type(value).__eq__ is object.__eq__ else value for key, value in kwargs.items()}
