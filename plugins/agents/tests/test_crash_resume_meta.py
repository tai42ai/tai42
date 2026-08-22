"""The ``crash_resume`` setting reaches the agent binding as registration meta.

Both durable-session agents DECLARE their ``crash_resume`` setting to the skeleton at
registration as ``meta={"tai42/crash_resume": <setting>}`` on the run tool, threaded through the
generic ``agents.agent(name, tags=..., meta=...)`` passthrough. Importing each agent module runs
its ``@tai42_app.agents.agent(...)`` decorator through the recording app bound in ``conftest``,
which records the declared meta by name — so this asserts the exact key/shape the skeleton's
run-dispatch seam reads (``tai42/crash_resume``) carries the live setting value.
"""

from __future__ import annotations

import importlib.util

import pytest
from pydantic import ValidationError

from tai42_agents.claude_code import agent as claude_agent
from tai42_agents.claude_code.settings import (
    ClaudeCodeSettings,
    claude_code_crash_resume,
    claude_code_settings,
)
from tai42_agents.langchain_deep_agent import agent as _deep_agent  # noqa: F401
from tai42_agents.langchain_deep_agent.settings import langchain_deep_agent_settings

from .conftest import APP

# The platform-generic registration meta key the skeleton run-dispatch seam reads (the plan's
# ``tai42/*`` meta convention); the agents plugin cannot import the skeleton to share the constant.
_CRASH_RESUME_META_KEY = "tai42/crash_resume"

# The ``TAI_AGENTS_CLAUDE_*`` env vars whose ABSENCE the import-without-creds proof clears.
_CLAUDE_ENV_VARS = (
    "TAI_AGENTS_CLAUDE_API_KEY",
    "TAI_AGENTS_CLAUDE_OAUTH_TOKEN",
    "TAI_AGENTS_CLAUDE_SESSION_IMAGE",
    "TAI_AGENTS_CLAUDE_CRASH_RESUME",
)


@pytest.mark.parametrize(
    ("name", "setting"),
    [
        ("claude_code", claude_code_settings().crash_resume),
        ("langchain_deep_agent", langchain_deep_agent_settings().crash_resume),
    ],
)
def test_crash_resume_setting_reaches_the_binding_as_meta(name: str, setting: bool) -> None:
    # The recorded meta is exactly the one generic key, carrying the live setting value.
    assert APP.agents.meta[name] == {_CRASH_RESUME_META_KEY: setting}


def test_crash_resume_true_flows_through_the_registration_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lightweight ``claude_code_crash_resume`` read (the exact source the registration meta
    uses) honors ``TAI_AGENTS_CLAUDE_CRASH_RESUME`` — proving the True flow, not only the default
    False the seeded env exercises above."""
    assert claude_code_crash_resume() is False  # the seeded env default
    monkeypatch.setenv("TAI_AGENTS_CLAUDE_CRASH_RESUME", "true")
    assert claude_code_crash_resume() is True


def test_import_without_creds_succeeds_and_full_validation_still_raises_at_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§A1: importing ``claude_code.agent`` must NOT require any ``TAI_AGENTS_CLAUDE_*`` creds —
    the registration meta is sourced from the lightweight ``crash_resume`` read alone. The full
    ``ClaudeCodeSettings`` validation (exactly-one-auth + digest image) still fires LOUDLY at run
    start. Proven by executing a FRESH copy of the module source with the creds env cleared (its
    ``@tai42_app.agents.agent`` decorator runs against the bound recording app) — the canonical
    module and its ``ClaudeCodeAgent`` class are left UNTOUCHED — then restoring the recording
    app's registration.
    """
    # Snapshot the recording-app registration: executing the fresh copy re-runs the decorator, which
    # transiently overwrites the registry/meta with the fresh module's class instance; restore it so
    # no fresh-class state leaks into later tests (the canonical module is never reloaded, so its
    # ClaudeCodeAgent identity — which other test modules compare against — never diverges).
    original_agent = APP.agents.registry["claude_code"]
    original_meta = APP.agents.meta["claude_code"]
    for var in _CLAUDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Drop the cached full settings so a registration that (before the fix) read them would re-read
    # the now-cleared env and raise — the lightweight ``crash_resume`` reader is uncached and needs
    # no clearing, so this only sharpens the fail-before guard.
    claude_code_settings.cache_clear()
    spec = importlib.util.spec_from_file_location("_claude_code_reimport", claude_agent.__file__)
    assert spec is not None
    assert spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    try:
        # Executing the module body runs the decorator with NO creds env present; it must NOT raise,
        # and the crash_resume meta defaults to False when unset.
        spec.loader.exec_module(fresh)
        assert APP.agents.meta["claude_code"] == {_CRASH_RESUME_META_KEY: False}
        # The full config validation is unchanged — with no model credential it raises loudly, so
        # the loud error lands at run start (the first ``astream``/``run`` calls this), not import.
        with pytest.raises(ValidationError):
            ClaudeCodeSettings()  # type: ignore[call-arg]
    finally:
        claude_code_settings.cache_clear()
        APP.agents.registry["claude_code"] = original_agent
        APP.agents.meta["claude_code"] = original_meta
