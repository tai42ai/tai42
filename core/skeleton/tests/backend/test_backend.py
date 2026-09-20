"""Backend feature tests.

Conformance: the seam re-exports the contract ``Backend`` ABC, an incomplete
subclass cannot instantiate, and a complete subclass satisfies the contract.
"""

from __future__ import annotations

import pytest
from tai42_contract.backend import Backend as ContractBackend

from tai42_skeleton import backend as _skeleton_backend

# The seam re-exports the contract ``Backend`` ABC through ``tai42_skeleton``;
# reference it via the module so the identity check below stays meaningful.
Backend = _skeleton_backend.Backend

# --- conformance ----------------------------------------------------------


def test_seam_re_exports_contract_abc() -> None:
    assert Backend is ContractBackend


def test_incomplete_backend_cannot_instantiate() -> None:
    # ``launch`` is the sole abstract member of the Backend ABC — a subclass that
    # omits it cannot instantiate.
    class Partial(Backend):
        pass

    with pytest.raises(TypeError):
        Partial()  # pyright: ignore[reportAbstractUsage]


def test_complete_backend_satisfies_contract() -> None:
    # The task backend carries task execution only — ``launch`` is the whole ABC;
    # fleet fan-out is the app's worker bus, not a backend surface.
    class Dummy(Backend):
        async def launch(self, args) -> None:
            return None

    backend = Dummy()
    assert isinstance(backend, Backend)
    assert isinstance(backend, ContractBackend)
