"""The pure door-contract evaluator: render + evaluate the four jqs with ``$parked`` bound.

``evaluate_door_contract`` is a pure function of its arguments with respect to the interaction store
and the ambient context — the parked list is INJECTED. Each declared expression renders through the
bound app's resource manager and evaluates over the door's input document.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.interactions import ResumeItem, TakeItem
from tai42_contract.interactions.door_contract import DoorContractError, ParkableDoorMixin
from tai42_contract.template import TemplatedText

from tai42_kit.interactions import DOOR_START_DEFAULT, evaluate_door_contract


class _FakeResourceManager:
    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        assert text.content is not None
        return text.content


@pytest.fixture
def bound_app() -> Iterator[None]:
    with tai42_app.bound(SimpleNamespace(storage=SimpleNamespace(resource_manager=_FakeResourceManager()))):
        yield


_PARKED = [{"id": "i-1", "status": "asking", "to": "caller", "asked_by": ["main"]}]


async def test_no_start_expr_yields_the_default_sentinel(bound_app) -> None:
    outcome = await evaluate_door_contract(ParkableDoorMixin(), {"a": 1}, [])
    assert outcome.start is DOOR_START_DEFAULT
    assert outcome.cancel == []
    assert outcome.resume == []
    assert outcome.extras == {}


async def test_start_expr_builds_kwargs_and_reads_parked(bound_app) -> None:
    contract = ParkableDoorMixin(
        start_expr=TemplatedText(content="{payload: .msg, waiting: ($parked | length)}"),
        extras_expr=TemplatedText(content="{warm: .msg}"),
    )
    outcome = await evaluate_door_contract(contract, {"msg": "hi"}, _PARKED)
    assert outcome.start == {"payload": "hi", "waiting": 1}
    assert outcome.extras == {"warm": "hi"}


async def test_start_expr_null_starts_nothing(bound_app) -> None:
    contract = ParkableDoorMixin(start_expr=TemplatedText(content="null"))
    outcome = await evaluate_door_contract(contract, {}, [])
    assert outcome.start is None


async def test_cancel_and_resume_parsed_from_parked(bound_app) -> None:
    contract = ParkableDoorMixin(
        cancel_expr=TemplatedText(content="[$parked[].id]"),
        resume_expr=TemplatedText(content='{id: $parked[0].id, payload: {answer: "yes"}}'),
    )
    outcome = await evaluate_door_contract(contract, {}, _PARKED)
    assert outcome.cancel == ["i-1"]
    assert outcome.resume == [ResumeItem(id="i-1", payload={"answer": "yes"})]


async def test_resume_bare_id_is_a_take(bound_app) -> None:
    contract = ParkableDoorMixin(resume_expr=TemplatedText(content="$parked[0].id"))
    outcome = await evaluate_door_contract(contract, {}, _PARKED)
    assert outcome.resume == [TakeItem(id="i-1")]


async def test_non_object_start_raises(bound_app) -> None:
    contract = ParkableDoorMixin(start_expr=TemplatedText(content='"a string"'))
    with pytest.raises(DoorContractError):
        await evaluate_door_contract(contract, {}, [])


async def test_multi_value_emit_raises(bound_app) -> None:
    # A door action is ONE value; a jq that streams several is the author's error.
    contract = ParkableDoorMixin(cancel_expr=TemplatedText(content=".ids[]"))
    with pytest.raises(DoorContractError):
        await evaluate_door_contract(contract, {"ids": ["a", "b"]}, [])
