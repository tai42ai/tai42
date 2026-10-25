"""``ReferenceHolder`` and ``ComposedToolRef``: frozen, closed models a referee and an extractor answer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract import tools
from tai42_contract.tools import ComposedToolRef, ReferenceHolder


def test_reference_holder_round_trips():
    holder = ReferenceHolder(kind="preset version", name="p", locator={"version": 2}, summary="preset 'p' version 2")
    assert ReferenceHolder.model_validate(holder.model_dump()) == holder
    assert ReferenceHolder.model_validate_json(holder.model_dump_json()) == holder


def test_reference_holder_locator_defaults_empty():
    assert ReferenceHolder(kind="hook", name="h", summary="hook 'h'").locator == {}


@pytest.mark.parametrize("field", ["kind", "name", "summary"])
def test_reference_holder_refuses_an_empty_field(field: str):
    values = {"kind": "hook", "name": "h", "summary": "hook 'h'", field: ""}
    with pytest.raises(ValidationError):
        ReferenceHolder.model_validate(values)


def test_reference_holder_refuses_an_unknown_key():
    with pytest.raises(ValidationError):
        ReferenceHolder.model_validate({"kind": "hook", "name": "h", "summary": "hook 'h'", "extra": 1})


def test_reference_holder_is_frozen():
    holder = ReferenceHolder(kind="hook", name="h", summary="hook 'h'")
    with pytest.raises(ValidationError):
        holder.name = "other"  # type: ignore[misc]


def test_composed_tool_ref_round_trips():
    ref = ComposedToolRef(name="echo", site="step 'a'", tool_kwargs={"text": "x"})
    assert ComposedToolRef.model_validate(ref.model_dump()) == ref
    assert ComposedToolRef.model_validate_json(ref.model_dump_json()) == ref


def test_composed_tool_ref_absent_site_and_kwargs_are_none():
    ref = ComposedToolRef(name="echo")
    assert ref.site is None
    assert ref.tool_kwargs is None


def test_composed_tool_ref_refuses_an_empty_site():
    with pytest.raises(ValidationError):
        ComposedToolRef(name="echo", site="")


def test_composed_tool_ref_refuses_an_empty_name():
    with pytest.raises(ValidationError):
        ComposedToolRef(name="")


def test_composed_tool_ref_refuses_an_unknown_key():
    with pytest.raises(ValidationError):
        ComposedToolRef.model_validate({"name": "echo", "where": "x"})


def test_models_and_cascade_type_are_exported():
    for name in ("ReferenceHolder", "ComposedToolRef", "ToolDeleteCascade"):
        assert name in tools.__all__
