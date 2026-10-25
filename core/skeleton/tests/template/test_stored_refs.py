"""The readers answering the stored template ids a stored model names, one ``TemplateRef`` per field."""

from __future__ import annotations

import pytest
from tai42_contract.conversations import ApiConversationRoute
from tai42_contract.hooks import HookParams, HookSubject
from tai42_contract.interactions import ParkableDoorMixin
from tai42_contract.template import TemplatedText
from tai42_contract.tools import TemplateRef

from tai42_skeleton.template.stored_refs import (
    condition_template_refs,
    door_contract_template_refs,
    hook_template_refs,
    route_template_refs,
)

_DOOR_SITES = ["start_expr", "cancel_expr", "resume_expr", "extras_expr"]


def _by_id(site: str) -> TemplatedText:
    return TemplatedText(id=f"jq/{site}")


def _inline() -> TemplatedText:
    return TemplatedText(content=".a")


def _refs(sites: list[str]) -> list[TemplateRef]:
    return [TemplateRef(id=f"jq/{site}", site=site) for site in sites]


def _hook(subject_key: TemplatedText, **fields: TemplatedText) -> HookParams:
    return HookParams(
        name="h",
        topic="events",
        tool="forward",
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        subject=HookSubject(target_kind="tool", target_name="forward", kind="person", key_expr=subject_key),
        **fields,  # type: ignore[arg-type]
    )


def _route(**fields: TemplatedText) -> ApiConversationRoute:
    return ApiConversationRoute(
        route_name="r",
        door="api",
        target_kind="tool",
        target_name="forward",
        execution_key="k-fire",
        callback_url="https://example.com/cb",
        execution_key_fingerprint="fp-fire",
        **fields,  # type: ignore[arg-type]
    )


def test_the_door_contract_reader_answers_its_four_expressions_in_field_order() -> None:
    contract = ParkableDoorMixin(**{site: _by_id(site) for site in _DOOR_SITES})
    assert door_contract_template_refs(contract) == _refs(_DOOR_SITES)


def test_the_hook_reader_answers_condition_then_the_subject_key_then_the_door_expressions() -> None:
    hook = _hook(
        TemplatedText(id="jq/subject.key_expr"),
        condition=_by_id("condition"),
        **{site: _by_id(site) for site in _DOOR_SITES},
    )
    assert hook_template_refs(hook) == _refs(["condition", "subject.key_expr", *_DOOR_SITES])


def test_the_route_reader_answers_the_door_expressions_then_the_reply() -> None:
    route = _route(reply_expr=_by_id("reply_expr"), **{site: _by_id(site) for site in _DOOR_SITES})
    assert route_template_refs(route) == _refs([*_DOOR_SITES, "reply_expr"])


def test_an_inline_field_answers_nothing() -> None:
    inline = {site: _inline() for site in _DOOR_SITES}
    assert door_contract_template_refs(ParkableDoorMixin(**inline)) == []
    assert hook_template_refs(_hook(_inline(), condition=_inline(), **inline)) == []
    assert route_template_refs(_route(reply_expr=_inline(), **inline)) == []
    assert condition_template_refs(_inline()) == []
    assert condition_template_refs(None) == []


def test_the_condition_reader_answers_the_same_ref_for_the_model_and_the_stored_mapping() -> None:
    expected = [TemplateRef(id="jq/acl", site="condition")]
    assert condition_template_refs(TemplatedText(id="jq/acl")) == expected
    assert condition_template_refs({"id": "jq/acl", "kwargs": {"tier": "pro"}}) == expected


def test_the_condition_reader_raises_over_a_malformed_mapping() -> None:
    with pytest.raises(ValueError, match="takes 'content' or 'id', not both"):
        condition_template_refs({"id": "jq/acl", "content": ".a"})
