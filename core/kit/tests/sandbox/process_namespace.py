"""The marks of a test module that runs the exec group scripts under a real shell.

The scripts' ``/proc`` scan reads every process of the exec's user that started after the exec's
wrapper, as it does inside the exec's own container. On a shared host that set holds strangers'
processes too, so these modules run only in a process namespace of their own: a container, with
``TAI42_KIT_PROCESS_NAMESPACE=1`` set.
"""

from __future__ import annotations

import os

import pytest

PROCESS_NAMESPACE_MARKS = [
    pytest.mark.process_namespace,
    pytest.mark.skipif(
        os.environ.get("TAI42_KIT_PROCESS_NAMESPACE") not in ("1", "true", "True"),
        reason=(
            "TAI42_KIT_PROCESS_NAMESPACE is not set: the exec scripts' /proc scan must see only the "
            "test's own processes, so these tests run in a process namespace of their own; run "
            "`pytest -m process_namespace` in a `docker run --init` container (CONTRIBUTING.md)"
        ),
    ),
]
