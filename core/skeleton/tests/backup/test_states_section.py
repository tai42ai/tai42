"""The ``states`` backup section — the feature gate on export/import, the payload version
guard, and the registration seam. The full data round-trip (export → import through the
facet doors) needs a live store and is exercised on the real stack in WP-9c e2e."""

from __future__ import annotations

import pytest

from tai42_skeleton.states import backup as backup_mod
from tai42_skeleton.states.backup import export_states, import_states, register_states_backup_section


async def test_export_empty_when_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_mod, "states_store_configured", lambda: False)
    payload = await export_states()
    assert payload == {"version": 1, "modules": [], "declarations": [], "mounts": [], "aliases": [], "records": []}


async def test_import_refuses_when_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_mod, "states_store_configured", lambda: True)
    # gate check comes after version validation, so drive with a valid version
    monkeypatch.setattr(backup_mod, "states_store_configured", lambda: False)
    with pytest.raises(RuntimeError, match="bind the 'states' component"):
        await import_states({"version": 1})


@pytest.mark.parametrize("version", [0, "1", None, -1])
async def test_import_refuses_invalid_version(version) -> None:
    with pytest.raises(ValueError, match="no valid version"):
        await import_states({"version": version})


async def test_import_refuses_newer_version(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="newer than this build"):
        await import_states({"version": 99})


def test_register_states_backup_section() -> None:
    calls: list[tuple] = []

    class _Registry:
        def register_section(self, name, exporter, importer, *, secret=False):
            calls.append((name, exporter, importer, secret))

    register_states_backup_section(_Registry())
    assert calls == [("states", export_states, import_states, False)]
