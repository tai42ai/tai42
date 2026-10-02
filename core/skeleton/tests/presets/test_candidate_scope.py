"""The candidate-scope override: the active-body read returns an UNSAVED candidate inside a re-check
scope, the committed body outside it, and the scope never leaks past its ``with`` block."""

from __future__ import annotations

import pytest
from tai42_contract.presets import PresetBody
from tai42_contract.presets.errors import PresetNotFoundError
from tai42_contract.versioning.errors import DocumentNotFoundError

from tai42_skeleton.presets.candidate_scope import candidate_bodies
from tai42_skeleton.presets.store import PresetStoreView

pytestmark = pytest.mark.asyncio


class _FakeVersionedStore:
    """The slice of the generic versioned store ``PresetStoreView.get_active_body`` reads."""

    def __init__(self, bodies: dict[str, dict]) -> None:
        self._bodies = bodies

    async def get_active_body(self, kind: str, name: str) -> dict:
        if name not in self._bodies:
            raise DocumentNotFoundError(kind, name)
        return self._bodies[name]


def _body(marker: str) -> PresetBody:
    return PresetBody(base_tool="composer", description="d", fixed_kwargs={"marker": marker})


async def test_get_active_body_returns_the_candidate_inside_the_scope_and_committed_outside():
    store = PresetStoreView(_FakeVersionedStore({"p": _body("committed").model_dump()}))  # pyright: ignore[reportArgumentType]

    assert (await store.get_active_body("p")).fixed_kwargs["marker"] == "committed"

    candidate = _body("candidate")
    with candidate_bodies({"p": candidate}):
        assert (await store.get_active_body("p")).fixed_kwargs["marker"] == "candidate"
        # A name the scope does not override still reads the committed store (a miss raises as usual).
        with pytest.raises(PresetNotFoundError):
            await store.get_active_body("other")

    # The override is cleared on exit — no leak into any later read.
    assert (await store.get_active_body("p")).fixed_kwargs["marker"] == "committed"


async def test_the_scope_clears_even_on_an_exception():
    store = PresetStoreView(_FakeVersionedStore({"p": _body("committed").model_dump()}))  # pyright: ignore[reportArgumentType]

    with pytest.raises(RuntimeError), candidate_bodies({"p": _body("candidate")}):
        raise RuntimeError("re-check blew up")

    # The override is reset by the context manager's ``finally`` despite the exception.
    assert (await store.get_active_body("p")).fixed_kwargs["marker"] == "committed"
