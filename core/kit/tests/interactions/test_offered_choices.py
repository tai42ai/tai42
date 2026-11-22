"""The pick rule for replaced choice lists: the string-form match and the picks a new list keeps."""

from __future__ import annotations

from tai42_kit.interactions import find_option, offered_picks

_LIST = [{"value": "b", "label": "Beta"}, {"value": "c"}]


def test_find_option_matches_a_pick_by_its_string_form():
    assert find_option(_LIST, "b") == {"value": "b", "label": "Beta"}
    assert find_option([{"value": "2"}], 2) == {"value": "2"}


def test_find_option_is_none_for_a_pick_the_list_does_not_offer():
    assert find_option(_LIST, "a") is None
    assert find_option([], "a") is None


def test_offered_picks_removes_a_single_choice_its_new_list_does_not_offer():
    assert offered_picks({"plan": "a", "note": "hi"}, {"plan": _LIST}) == {"note": "hi"}


def test_offered_picks_keeps_a_single_choice_its_new_list_offers():
    assert offered_picks({"plan": "c"}, {"plan": _LIST}) == {"plan": "c"}


def test_offered_picks_keeps_the_entries_of_a_multiple_choice_its_new_list_offers():
    assert offered_picks({"tags": ["a", "b", "c"]}, {"tags": _LIST}) == {"tags": ["b", "c"]}
    assert offered_picks({"tags": ["a"]}, {"tags": _LIST}) == {"tags": []}


def test_offered_picks_keeps_a_field_without_a_new_list_as_entered():
    assert offered_picks({"plan": "a"}, {"other": _LIST}) == {"plan": "a"}


def test_offered_picks_does_not_change_its_input():
    entered = {"tags": ["a", "b"]}
    offered_picks(entered, {"tags": _LIST})
    assert entered == {"tags": ["a", "b"]}
