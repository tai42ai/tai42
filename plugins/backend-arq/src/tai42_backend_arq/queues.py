"""Worker queue labels and the arq queue key each derives.

A label (letters, digits, ``_``, ``-``) names a worker pool. Its queue key is
``<ARQ_QUEUE_NAME>.<label>``: a label holds no ``.`` or ``:``, so a derived key never
equals the settings queue, the queue registry key or an arq per-queue key.
"""

from __future__ import annotations

import re

from tai42_backend_arq.settings import arq_settings

QUEUE_LABEL_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def check_queue_label(label: str) -> str:
    """Return ``label``, or raise ``ValueError`` when it is not letters, digits, ``_`` or ``-``."""
    if QUEUE_LABEL_PATTERN.fullmatch(label) is None:
        raise ValueError(
            f"queue label {label!r} must match [A-Za-z0-9_-]+ (letters, digits, '_' or '-'); "
            "omit it to use the settings queue"
        )
    return label


def derived_queue(label: str | None) -> str:
    """The arq queue key for ``label``; ``None`` is the settings queue (``ARQ_QUEUE_NAME``)."""
    settings_queue = arq_settings().queue_name
    if label is None:
        return settings_queue
    return f"{settings_queue}.{check_queue_label(label)}"
