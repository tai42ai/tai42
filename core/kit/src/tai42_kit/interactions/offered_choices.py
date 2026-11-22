"""The rule a form's picks follow when a field's choice list is set per send and replaced while the form is open.

A pick and a choice match by their string forms, the form a choice's ``value`` is drawn and submitted
in. When an update gives a field a new list, the pick the field holds keeps only what that list
offers, on every channel, before the update's own values are laid on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["find_option", "offered_picks"]


def find_option[O: Mapping[str, Any]](options: Sequence[O], value: Any) -> O | None:
    """The option a pick of ``value`` selects, or ``None`` when ``options`` does not offer it.

    Each option carries its choice under ``value``; a pick and an option match by their string forms.
    """
    picked = str(value)
    return next((option for option in options if str(option["value"]) == picked), None)


def offered_picks(entered: Mapping[str, Any], options: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """``entered`` keeping only the picks the choice lists in ``options`` offer.

    A single choice its list does not offer is removed (the field is unfilled); a multiple choice (a
    list) keeps the entries its list offers, matched by :func:`find_option`. A field with no list in
    ``options`` keeps its entry as entered.
    """
    kept: dict[str, Any] = {}
    for name, value in entered.items():
        option_list = options.get(name)
        if option_list is None:
            kept[name] = value
        elif isinstance(value, list):
            kept[name] = [item for item in value if find_option(option_list, item) is not None]
        elif find_option(option_list, value) is not None:
            kept[name] = value
    return kept
