"""The ambient call frame: the three chain forms (PUSH / SET / DOOR), the extras
channel, and the run-delivery identity + address it mints once at an outermost run
start and every nested dispatch and forked branch inherits unchanged.
"""

from __future__ import annotations

import contextvars

from tai42_contract.interactions.continuation import reset_park_completion, set_park_completion
from tai42_contract.tools import (
    current_call_chain,
    current_extras,
    get_run_delivery,
    get_run_delivery_id,
    tool_call_frame,
)


def test_push_form_appends_the_name_outermost_first() -> None:
    assert current_call_chain() == ()
    with tool_call_frame("main"):
        assert current_call_chain() == ("main",)
        with tool_call_frame("sub"):
            assert current_call_chain() == ("main", "sub")
            with tool_call_frame("subsub"):
                assert current_call_chain() == ("main", "sub", "subsub")
        assert current_call_chain() == ("main",)
    assert current_call_chain() == ()


def test_set_form_replaces_the_chain_and_pushes_no_name() -> None:
    # A continuation dispatch restores the parked run's chain and its own tool name
    # is absent for that dispatch — even though a name is passed.
    with tool_call_frame("resume_tool", continues_chain=["main", "sub"]):
        assert current_call_chain() == ("main", "sub")
        # A nested ordinary dispatch pushes its own name on top of the restored chain.
        with tool_call_frame("subsub"):
            assert current_call_chain() == ("main", "sub", "subsub")
    assert current_call_chain() == ()


def test_set_form_with_empty_chain_restores_an_empty_chain() -> None:
    with tool_call_frame("resume_tool", continues_chain=[]):
        assert current_call_chain() == ()
    assert current_call_chain() == ()


def test_door_form_pushes_nothing_and_the_inner_dispatch_pushes_the_entry() -> None:
    # A door frame owns the run-delivery context but adds no chain entry; the inner
    # tool dispatch pushes the single entry itself (so a route-started tool's first
    # ask records ``[target]``, never ``[target, target]``).
    with tool_call_frame():
        assert current_call_chain() == ()
        with tool_call_frame("target"):
            assert current_call_chain() == ("target",)
    assert current_call_chain() == ()


def test_extras_default_empty_and_bound_value() -> None:
    assert current_extras() == {}
    with tool_call_frame("main", {"budget": 3}):
        assert current_extras() == {"budget": 3}
        with tool_call_frame("sub"):
            # A frame that binds no extras leaves the inner extras unset (not inherited).
            assert current_extras() == {}
        assert current_extras() == {"budget": 3}
    assert current_extras() == {}


def test_outermost_start_mints_a_run_delivery_id_and_reads_the_door_address() -> None:
    assert get_run_delivery_id() is None
    completion = set_park_completion("deliver_tool_completion", {"delivery_thread_id": "bridge:r:a"})
    try:
        with tool_call_frame("main"):
            run_id = get_run_delivery_id()
            assert run_id is not None
            # The address is the door's bound completion, read once at the start.
            assert get_run_delivery() == ("deliver_tool_completion", {"delivery_thread_id": "bridge:r:a"})
    finally:
        reset_park_completion(completion)
    assert get_run_delivery_id() is None
    assert get_run_delivery() is None


def test_receiverless_door_mints_an_id_with_no_address() -> None:
    # No completion bound (or a completion naming no tool) → a receiver-less run:
    # an id is minted but the delivery address is None.
    with tool_call_frame("main"):
        assert get_run_delivery_id() is not None
        assert get_run_delivery() is None


def test_door_form_start_mints_too() -> None:
    with tool_call_frame():
        assert get_run_delivery_id() is not None


def test_nested_frame_inherits_the_same_id_and_never_re_mints() -> None:
    with tool_call_frame("main"):
        outer = get_run_delivery_id()
        with tool_call_frame("sub"):
            assert get_run_delivery_id() == outer
            with tool_call_frame("subsub"):
                assert get_run_delivery_id() == outer


def test_a_forked_branch_reads_the_id_set_before_the_fork() -> None:
    # A value bound before a fork is inherited on the ContextVar copy, so every
    # parallel branch of one run reads the SAME id.
    with tool_call_frame("main"):
        run_id = get_run_delivery_id()

        seen: list[str | None] = []

        def _branch() -> None:
            # A nested dispatch inside the forked branch still inherits the one id.
            with tool_call_frame("branch"):
                seen.append(get_run_delivery_id())

        for _ in range(2):
            contextvars.copy_context().run(_branch)
        assert seen == [run_id, run_id]


def test_a_set_frame_never_mints_and_inherits_an_ambient_context() -> None:
    # A continuation SET frame is not a run start: with an ambient context already
    # bound it adopts it; with none it mints nothing (the resume runner re-establishes
    # the stored id first — proven here by the absence of a mint).
    assert get_run_delivery_id() is None
    with tool_call_frame("resume_tool", continues_chain=["main"]):
        assert get_run_delivery_id() is None
    # With an ambient context, a SET frame inside it inherits rather than re-mints.
    with tool_call_frame("outer"):
        ambient = get_run_delivery_id()
        with tool_call_frame("resume_tool", continues_chain=["main"]):
            assert get_run_delivery_id() == ambient


def test_a_start_shaped_frame_does_not_re_mint_when_a_context_is_already_ambient() -> None:
    # The route door opens a minting DOOR frame, then the target's own dispatch opens
    # a start-shaped PUSH frame onto the still-empty chain; the guard keeps it from
    # minting a second id.
    with tool_call_frame():  # the door mints
        door_id = get_run_delivery_id()
        assert current_call_chain() == ()
        with tool_call_frame("target"):  # start-shaped (empty prior chain) but ambient present
            assert get_run_delivery_id() == door_id


def test_two_independent_runs_mint_different_ids() -> None:
    with tool_call_frame("run_a"):
        first = get_run_delivery_id()
    with tool_call_frame("run_b"):
        second = get_run_delivery_id()
    assert first is not None
    assert second is not None
    assert first != second
