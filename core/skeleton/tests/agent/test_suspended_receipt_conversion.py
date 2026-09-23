"""The agent tool-face receipt→sentinel conversion carries both id lists of a parked super-step."""

from __future__ import annotations

from tai42_skeleton.agent.binding import _suspended_interaction_from_receipt


def test_conversion_carries_both_id_lists_for_a_mixed_super_step() -> None:
    # A super-step that parked a user ask AND a caller ask at once crosses the single-id tool-face
    # as ONE sentinel carrying every parked id plus the caller subset, so the platform's visit
    # partitions caller from user asks. The single-id key is the first of them.
    receipt = {
        "status": "suspended",
        "interaction_ids": ["i-user", "i-caller"],
        "caller_interaction_ids": ["i-caller"],
        "thread_id": "t1",
        "expiry_at": None,
    }
    sentinel = _suspended_interaction_from_receipt(receipt)
    assert sentinel.interaction_id == "i-user"
    assert sentinel.interaction_ids == ["i-user", "i-caller"]
    assert sentinel.caller_interaction_ids == ["i-caller"]
    # A park the platform re-normalises, never adopts: no resume owner.
    assert sentinel.resume_owner is None


def test_conversion_of_a_user_only_park_has_an_empty_caller_subset() -> None:
    receipt = {"status": "suspended", "interaction_ids": ["i1"], "thread_id": "t1", "expiry_at": None}
    sentinel = _suspended_interaction_from_receipt(receipt)
    assert sentinel.interaction_ids == ["i1"]
    assert sentinel.caller_interaction_ids == []
