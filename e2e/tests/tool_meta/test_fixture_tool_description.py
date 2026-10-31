"""A fixture probe tool's declared description is served on its ``GET /api/tools/tags`` row.

Asserted over the RUNNING stack, so it proves the docstring survives the whole
registration → serve path that the Studio tool choosers search. Backend-invariant, so
it runs once on the default leg."""

from __future__ import annotations

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = [pytest.mark.backendless, pytest.mark.needs("probe-tools")]

# ``e2e_echo``'s docstring, cleaned the way the tool registration reads it (``inspect.getdoc``).
_E2E_ECHO_DESCRIPTION = (
    "Return ``payload`` unchanged — the default subject for extension\n"
    "attachment (prometheus / batch / proxy branches)."
)


async def test_fixture_tool_row_carries_its_declared_description(core_stack: TaiStack) -> None:
    rows = {entry["name"]: entry for entry in await core_stack.api().get("/api/tools/tags")}

    assert "e2e_echo" in rows, "the e2e_echo probe tool is absent from the served tags rows"
    assert rows["e2e_echo"]["description"] == _E2E_ECHO_DESCRIPTION
