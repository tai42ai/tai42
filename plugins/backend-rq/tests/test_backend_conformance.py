"""The shared-base certification, run against the shipped RQ backend.

An empty problem list is the claim that this plugin writes only its runtime
classes: the readiness gate, the composed warm/cold shutdown wiring, the
drain-on-cancellation and the off-loop run all come from the base, no runtime
installs a signal handler of its own, and the consuming runtime carries the
canonical name the host sniffs argv for before this module is even imported.
"""

from __future__ import annotations

from typing import Any

from tai42_kit.backend import check_backend_declarations

from tai42_backend_rq.backend import RqBackend

_RqBackendCls: Any = RqBackend


def test_the_rq_backend_is_certified() -> None:
    assert check_backend_declarations(_RqBackendCls()) == []
