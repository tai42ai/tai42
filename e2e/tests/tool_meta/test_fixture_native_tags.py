"""Every e2e fixture probe tool declares the native ``e2e`` tag at registration.

Asserted over the RUNNING stack's ``GET /api/tools/tags`` — the same served map
the Studio tools screen merges with the tool_meta overlay tags — so this proves
the native tag survives the whole registration → serve path, not merely the
decorator literal. Backend-invariant, so it runs once on the default leg."""

from __future__ import annotations

import pytest

from tai42_e2e.stack import TaiStack

# The tag reads identically under every backend variant, so the non-default backend
# legs buy nothing by re-running it.
pytestmark = pytest.mark.backendless

# The probe tools registered by ``tai42_e2e_fixtures.tools`` — every one carries the
# native ``e2e`` tag (mirroring how the shipped fleet is tagged).
_FIXTURE_TOOLS = frozenset(
    {
        "e2e_echo",
        "e2e_worker_info",
        "e2e_settings_snapshot",
        "e2e_record",
        "e2e_sleep",
        "e2e_slow_task",
        "e2e_drain_probe",
        "e2e_fail",
        "e2e_external_link",
        "e2e_http_probe",
        "run_tool",
        "e2e_resolve_connector",
    }
)


async def test_fixture_tools_carry_the_native_e2e_tag(core_stack: TaiStack) -> None:
    tags_by_name = {entry["name"]: entry["tags"] for entry in await core_stack.api().get("/api/tools/tags")}

    missing = _FIXTURE_TOOLS - tags_by_name.keys()
    assert not missing, f"fixture probe tools absent from the served tag map: {sorted(missing)}"

    untagged = sorted(name for name in _FIXTURE_TOOLS if "e2e" not in tags_by_name[name])
    assert not untagged, f"fixture probe tools missing the native 'e2e' tag: {untagged}"
