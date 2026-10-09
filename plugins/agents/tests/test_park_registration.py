"""The hidden park tools bind once per serving epoch; only the typed duplicate refusal is a no-op."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
from tai42_contract.tools import ToolAlreadyRegisteredError

from tai42_agents._internal.park.chain import CHAINED_PARK_DELIVERY_TOOL_NAME, register_chained_park_tool
from tai42_agents._internal.park.resume_tool import AGENT_RESUME_TOOL_NAME, register_agent_resume_tool

from .conftest import APP

_HELPERS = [
    (register_agent_resume_tool, AGENT_RESUME_TOOL_NAME, "tai42_agents._internal.park.resume_tool"),
    (register_chained_park_tool, CHAINED_PARK_DELIVERY_TOOL_NAME, "tai42_agents._internal.park.chain"),
]


class _Surface:
    """A tool registrar that binds each name once and refuses a second bind the platform's way."""

    def __init__(self, refusal: Callable[[str], Exception]) -> None:
        self.bound: list[str] = []
        self._refusal = refusal

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            name = kwargs["name"]
            if name in self.bound:
                raise self._refusal(name)
            self.bound.append(name)
            return func

        return decorator


@pytest.mark.parametrize(("register", "name", "logger"), _HELPERS)
def test_a_second_call_binds_nothing_and_logs_the_no_op(
    register: Callable[[], None], name: str, logger: str, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    surface = _Surface(ToolAlreadyRegisteredError)
    monkeypatch.setattr(APP.tools, "tool", surface.tool)
    with caplog.at_level(logging.DEBUG, logger=logger):
        register()
        register()
    assert surface.bound == [name]
    assert any("registration is a no-op" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(("register", "name", "logger"), _HELPERS)
def test_any_other_value_error_propagates_whatever_its_text(
    register: Callable[[], None], name: str, logger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = _Surface(lambda tool: ValueError(f"Component already exists: {tool}"))
    monkeypatch.setattr(APP.tools, "tool", surface.tool)
    register()
    with pytest.raises(ValueError, match="already exists") as caught:
        register()
    assert not isinstance(caught.value, ToolAlreadyRegisteredError)
