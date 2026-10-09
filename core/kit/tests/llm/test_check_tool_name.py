"""The one bound-tool name rule: a name the strictest provider accepts, refused (never truncated) past it."""

from __future__ import annotations

import pytest
from tai42_contract.agent import TOOL_NAME_MAX_LEN

from tai42_kit.llm import ToolNameTooLongError, check_tool_name


def test_a_name_at_the_bound_passes():
    check_tool_name("t" * TOOL_NAME_MAX_LEN)


def test_a_name_past_the_bound_raises_naming_the_tool():
    name = "t" * (TOOL_NAME_MAX_LEN + 1)
    with pytest.raises(ToolNameTooLongError) as caught:
        check_tool_name(name)
    assert str(caught.value) == f"tool name {name!r} is 65 characters; a bound tool name may be at most 64"
    assert isinstance(caught.value, ValueError)
