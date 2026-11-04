"""The ``Backend`` ABC: what every execution backend declares."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from tai42_contract.backend import Backend


def test_a_backend_declares_its_drain_bound() -> None:
    class _Declared(Backend):
        @property
        def drain_timeout(self) -> float:
            return 630.0

        async def launch(self, args: Sequence[str]) -> None:
            return None

    assert _Declared().drain_timeout == 630.0


def test_a_backend_declaring_no_drain_bound_cannot_be_built() -> None:
    class _Undeclared(Backend):
        async def launch(self, args: Sequence[str]) -> None:
            return None

    with pytest.raises(TypeError, match="drain_timeout"):
        _Undeclared()  # pyright: ignore[reportAbstractUsage]
