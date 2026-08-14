"""The shared-base certification, run against this backend.

An empty problem list is the claim that this plugin writes only its Celery
bindings and gets readiness gating, composed shutdown wiring,
drain-on-cancellation and pool turnover from the base — with no host-shaped code,
and no signal handler, of its own.
"""

from __future__ import annotations

from tai42_kit.backend import check_backend_declarations

from tai42_backend_celery.core.backend import CeleryBackend


def test_the_celery_backend_is_certified() -> None:
    assert check_backend_declarations(CeleryBackend()) == []
