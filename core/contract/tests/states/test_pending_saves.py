"""The pending-save shapes of the states contract: the commit result, the held-save models, the errors."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.states import (
    ApplyResult,
    DeferredCallRefusedError,
    HeldPendingSave,
    PruneResult,
    StateDeclaration,
    StateDeclarationSaved,
    StatePendingSaveFailedError,
    StatePendingSaveTimeoutError,
    StatesError,
    StateSubject,
    StateUnit,
    UnitCommitResult,
)

_SUBJECT = StateSubject(target_kind="tool", target_name="probe", kind="thread", key="t-1")


def test_a_commit_result_carries_the_provisional_results_and_the_pending_save():
    result = UnitCommitResult(
        results=[ApplyResult(applied=True, data={"a": 1}, seq=1.0)], outbox_id="17", deferred_calls=2
    )
    assert result.outbox_id == "17"
    assert result.deferred_calls == 2
    assert UnitCommitResult().model_dump() == {"results": [], "outbox_id": None, "deferred_calls": 0}


def test_a_commit_result_refuses_an_unknown_key():
    with pytest.raises(ValidationError):
        UnitCommitResult(surprise=True)  # type: ignore[call-arg]


def test_a_held_pending_save_is_frozen_and_forbids_extra_keys():
    held = HeldPendingSave(save_id="18", held_by="17", subjects=[_SUBJECT])
    with pytest.raises(ValidationError):
        held.save_id = "19"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        HeldPendingSave(save_id="18", held_by="17", subjects=[], extra=1)  # type: ignore[call-arg]


def test_a_prune_result_defaults_to_no_held_saves():
    result = PruneResult(pruned={"notes": 3})
    assert result.held == []
    assert PruneResult(pruned={}, held=[HeldPendingSave(save_id="1", held_by="1", subjects=[_SUBJECT])]).held


def test_a_saved_declaration_carries_the_declaration_and_its_held_saves():
    decl = StateDeclaration(
        name="notes", schema={"type": "object"}, subject_kinds=["thread"], default_subject_kind="thread"
    )
    saved = StateDeclarationSaved(declaration=decl, held=[])
    assert saved.declaration.name == "notes"
    with pytest.raises(ValidationError):
        saved.held = []  # type: ignore[misc]


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (StatePendingSaveFailedError("held", save_id="17"), ErrorKind.CONFLICT),
        (StatePendingSaveTimeoutError("slow", save_id="17"), ErrorKind.UNAVAILABLE),
        (DeferredCallRefusedError("bad"), ErrorKind.BAD_INPUT),
    ],
)
def test_the_pending_save_errors_stamp_their_kind(exc: Exception, kind: ErrorKind):
    assert isinstance(exc, StatesError)
    assert error_kind(exc) is kind


@pytest.mark.parametrize("cls", [StatePendingSaveFailedError, StatePendingSaveTimeoutError])
def test_a_pending_save_error_names_its_save_on_extra(cls: type[StatesError]):
    exc = cls("subject x has a failed pending save 17", save_id="17")  # type: ignore[call-arg]
    assert exc.extra == {"save_id": "17"}
    assert str(exc) == "subject x has a failed pending save 17"


def test_a_state_unit_defers_calls():
    class _Unit:
        async def stage(self, writes: list[Any]) -> list[ApplyResult]:  # pragma: no cover - shape only
            return []

        async def commit(self) -> UnitCommitResult:  # pragma: no cover
            return UnitCommitResult()

        async def discard(self) -> None:  # pragma: no cover
            return None

        def savepoint(self) -> Any:  # pragma: no cover
            raise AssertionError

        async def defer_call(
            self, tool: str, arguments: dict[str, Any], *, run_id: str | None = None
        ) -> None:  # pragma: no cover
            return None

    class _NoDefer:
        async def stage(self, writes: list[Any]) -> list[ApplyResult]:  # pragma: no cover
            return []

        async def commit(self) -> UnitCommitResult:  # pragma: no cover
            return UnitCommitResult()

        async def discard(self) -> None:  # pragma: no cover
            return None

        def savepoint(self) -> Any:  # pragma: no cover
            raise AssertionError

    assert isinstance(_Unit(), StateUnit)
    assert not isinstance(_NoDefer(), StateUnit)
