"""Worker queue labels and the queue name each derives.

A label (letters, digits, ``_``, ``-``) names a worker pool. Its queue is
``<settings queue>.<label>``: a label holds no ``.`` or ``:``, so a derived name never equals
the settings queue, nor any key a backend builds by suffixing the settings queue with ``:``.
A worker CLI takes the label through :func:`queue_label_option`.
"""

from __future__ import annotations

import re

import click

#: The grammar a queue label matches in full.
QUEUE_LABEL_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def check_queue_label(label: str) -> str:
    """Return ``label``, or raise ``ValueError`` when it is not letters, digits, ``_`` or ``-``."""
    if QUEUE_LABEL_PATTERN.fullmatch(label) is None:
        raise ValueError(
            f"queue label {label!r} must match [A-Za-z0-9_-]+ (letters, digits, '_' or '-'); "
            "omit it to use the settings queue"
        )
    return label


def derived_queue(settings_queue: str, label: str | None) -> str:
    """The queue name for ``label`` under ``settings_queue``; ``None`` is ``settings_queue`` itself."""
    if label is None:
        return settings_queue
    return f"{settings_queue}.{check_queue_label(label)}"


def queue_label_option(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """The click callback of a worker's ``--queue`` option: the checked label, or ``None`` when unset.

    A label outside the grammar raises ``click.BadParameter`` naming the option.
    """
    if value is None:
        return None
    try:
        return check_queue_label(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc
