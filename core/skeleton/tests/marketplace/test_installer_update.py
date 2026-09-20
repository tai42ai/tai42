"""The update flow: one-write replace, collision pre-flight, unwind escalation, upgrade-all."""

from __future__ import annotations

import importlib.metadata
from typing import Any

import pytest

from tai42_skeleton.marketplace import installer_update as installer_update_module
from tai42_skeleton.marketplace.errors import (
    InstallStateError,
    InstallUnwindError,
    MalformedRefError,
    ManifestCollisionError,
    OperationInProgressError,
    PipUnavailableError,
    RegistryResponseError,
)
from tai42_skeleton.operations._broadcast import FleetBroadcastError

from ._specs import make_resolved, make_spec
from .test_installer import (
    _GH_ARTIFACT,
    _GH_ARTIFACT_OLD,
    Harness,
    _assert_no_pip,
    _fake_verified_fetch,
    _tool_provides,
)

# -- update ------------------------------------------------------------------


async def test_update_happy_replaces_row_in_one_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    old = make_spec(version="1.0.0", provides=_tool_provides("pkg.old"))
    new = make_spec(version="2.0.0", provides=_tool_provides("pkg.new"))
    h = Harness(manifest={"tools": [{"title": "pkg.old", "module": "pkg.old"}]})
    h.store.preload(old, version="1.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0")

    result = await h.installer().update("tai42/toolbox")

    assert result["version"] == "2.0.0"
    # One RMW: old removed, new applied.
    assert h.svc.writes[-1]["tools"] == [{"title": "pkg.new", "module": "pkg.new"}]
    assert h.store.record_calls[-1][1] == "2.0.0"


async def test_update_same_version_is_state_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    spec = make_spec(version="1.0.0")
    h = Harness()
    h.store.preload(spec, version="1.0.0")
    h.registry.resolved = make_resolved(spec, version="1.0.0")
    with pytest.raises(InstallStateError, match=r"already at 1\.0\.0"):
        await h.installer().update("tai42/toolbox")


async def test_update_unknown_ref_is_not_installed() -> None:
    h = Harness()
    with pytest.raises(InstallStateError) as exc:
        await h.installer().update("tai42/gone")
    assert exc.value.not_installed is True


async def test_update_bad_ref_raises_malformed_ref_error() -> None:
    # A malformed ref is parsed BEFORE the store read, so it is the caller's typed
    # MalformedRefError (a 400) — matching install — never a phantom 404 from a
    # not-installed lookup on an unparseable ref.
    h = Harness()
    with pytest.raises(MalformedRefError, match="namespace/name"):
        await h.installer().update("noslash")


async def test_update_pipless_fails_after_resolve_for_packaged_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pip presence is now checked AFTER the resolve (the delivery form must be known
    # first — a descriptor-only spec needs no pip), so a pipless environment fails the
    # update of a PACKAGED spec, after the resolve counted, before any pip work.
    def _no_pip() -> None:
        raise PipUnavailableError("no pip")

    monkeypatch.setattr(installer_update_module, "ensure_pip_available", _no_pip)
    old = make_spec(version="1.0.0")
    new = make_spec(version="2.0.0")
    h = Harness()
    h.store.preload(old, version="1.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0")
    with pytest.raises(PipUnavailableError):
        await h.installer().update("tai42/toolbox")
    # The resolve ran (the pip check now follows it); no pip command was issued.
    assert h.registry.resolve_calls != []
    assert h.pip.calls == []


async def test_update_unwind_reinstalls_old_github_pin_through_verified_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    calls = _fake_verified_fetch(monkeypatch)
    old = make_spec(version="1.0.0")
    new = make_spec(version="2.0.0")
    h = Harness(manifest={"tools": [{"title": "pkg.tools.gen_uuid", "module": "pkg.tools.gen_uuid"}]})
    # The stored row is a github pin carrying the OLD artifact_ref + sha256, so the
    # unwind re-fetches and re-verifies exactly that old artifact.
    h.store.preload(
        old,
        version="1.0.0",
        source="github",
        repository_url="https://github.com/tai42ai/toolbox",
        tag="v1.0.0",
        artifact_ref=_GH_ARTIFACT_OLD,
        sha256="1" * 64,
    )
    h.registry.resolved = make_resolved(
        new,
        source="github",
        version="2.0.0",
        repository_url="https://github.com/tai42ai/toolbox",
        tag="v2.0.0",
        artifact_ref=_GH_ARTIFACT,
        sha256="2" * 64,
    )
    h.svc.fail_reload_on = {0}  # the update apply persists then its reload fails -> unwind

    with pytest.raises(FleetBroadcastError, match="reload failed"):
        await h.installer().update("tai42/toolbox")

    # Verified fetch: first the NEW pin (from resolve), then the OLD pin (from the
    # stored row's artifact_ref + sha256), never a mutable git+url clone.
    assert (calls[0]["artifact_ref"], calls[0]["sha256"]) == (_GH_ARTIFACT, "2" * 64)
    assert (calls[1]["artifact_ref"], calls[1]["sha256"]) == (_GH_ARTIFACT_OLD, "1" * 64)
    # pip installed each verified LOCAL tarball, in that order.
    assert h.pip.calls[0][-1] == str(calls[0]["path"])
    assert h.pip.calls[1][-1] == str(calls[1]["path"])
    assert not h.pip.calls[1][-1].startswith("git+")
    # Ordering: old-pin reinstall -> manifest restore apply (persist + reload-back).
    # The old wheel is back BEFORE the restore's reload loads the old manifest.
    tail = [e for e in h.events if e in ("cm:write", "pip", "reload")]
    # ... write(new), reload(fail), pip(old), write(saved), reload(back)
    assert tail[-3:] == ["pip", "cm:write", "reload"]


# -- update: collision pre-flight --------------------------------------------


async def test_update_same_module_rename_does_not_self_collide(monkeypatch: pytest.MonkeyPatch) -> None:
    # The new spec re-provides the SAME module the old spec already wrote to the
    # manifest (a version bump that keeps the module path). The pre-flight removes
    # the OLD spec's entries in memory BEFORE the collision check, so the shared
    # module must NOT be read as a self-collision — the update proceeds to pip.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    old = make_spec(version="1.0.0", provides=_tool_provides("pkg.same"))
    new = make_spec(version="2.0.0", provides=_tool_provides("pkg.same"))
    h = Harness(manifest={"tools": [{"title": "pkg.same", "module": "pkg.same"}]})
    h.store.preload(old, version="1.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0")

    result = await h.installer().update("tai42/toolbox")

    assert result["version"] == "2.0.0"
    assert h.pip.calls  # proceeded past the (non-)collision to the pip upgrade
    # One RMW keeps the single shared-module entry (removed then re-applied).
    assert h.svc.writes[-1]["tools"] == [{"title": "pkg.same", "module": "pkg.same"}]


async def test_update_genuine_collision_refuses_before_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    # The new spec's module collides with a FOREIGN manifest entry (one that does
    # not belong to the old spec, so removing the old entries does not clear it).
    # That is a real update-side collision: a 409 raised BEFORE any pip call.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    old = make_spec(version="1.0.0", provides=_tool_provides("pkg.old"))
    new = make_spec(version="2.0.0", provides=_tool_provides("pkg.foreign"))
    h = Harness(
        manifest={
            "tools": [
                {"title": "pkg.old", "module": "pkg.old"},
                {"title": "other-plugin", "module": "pkg.foreign"},
            ]
        }
    )
    h.store.preload(old, version="1.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0")
    with pytest.raises(ManifestCollisionError, match=r"pkg\.foreign"):
        await h.installer().update("tai42/toolbox")
    _assert_no_pip(h)


# -- update: unwind escalation -----------------------------------------------


async def test_update_unwind_reload_back_failure_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    # The update apply (call 0) persists then its reload fails, triggering the unwind;
    # the unwind's own restore apply (call 1) then fails its reload too. A failed
    # unwind sub-step escalates to InstallUnwindError carrying both the original step
    # error and the unwind error.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    old = make_spec(version="1.0.0", provides=_tool_provides("pkg.old"))
    new = make_spec(version="2.0.0", provides=_tool_provides("pkg.new"))
    h = Harness(manifest={"tools": [{"title": "pkg.old", "module": "pkg.old"}]})
    h.store.preload(old, version="1.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0")
    h.svc.fail_reload_on = {0, 1}  # forward update apply AND the unwind restore apply both fail their reload

    with pytest.raises(InstallUnwindError) as exc:
        await h.installer().update("tai42/toolbox")
    assert isinstance(exc.value.step_error, FleetBroadcastError)
    assert isinstance(exc.value.unwind_error, FleetBroadcastError)
    # The old pin WAS reinstalled before the failing restore apply (unwind reached it).
    assert h.pip.calls[-1][0] == "install"


# -- upgrade-all --------------------------------------------------------------


def _published_row(version: str, contract_range: str = ">=0.1,<1.0") -> dict[str, Any]:
    return {"version": version, "status": "published", "contract_range": contract_range}


async def test_upgrade_all_reports_every_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    # One batch, four refs, one of each outcome — and the report is COMPLETE:
    # the failed ref never truncates the entries after it.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    up = make_spec(name="up", package="pkg-up", provides=_tool_provides("pkg.up"))
    current = make_spec(name="current", package="pkg-current", provides=_tool_provides("pkg.current"))
    stuck = make_spec(name="stuck", package="pkg-stuck", provides=_tool_provides("pkg.stuck"))
    broken = make_spec(name="broken", package="pkg-broken", provides=_tool_provides("pkg.broken"))
    h = Harness()
    for spec in (up, current, stuck, broken):
        h.store.preload(spec, version="1.0.0")
    h.registry.versions_map = {
        "tai42/up": [_published_row("1.2.0"), _published_row("1.0.0")],
        "tai42/current": [_published_row("1.0.0"), _published_row("9.0.0", contract_range=">=9,<10")],
        "tai42/stuck": [_published_row("2.0.0", contract_range=">=9,<10")],
        "tai42/broken": RegistryResponseError("registry served garbage", status=None),
    }
    # The one genuinely-upgrading ref drives the ordinary update flow, which
    # re-resolves its picked pin.
    h.registry.resolved = make_resolved(make_spec(name="up", package="pkg-up"), version="1.2.0")

    report = await h.installer().upgrade_all()

    by_ref = {entry["ref"]: entry for entry in report}
    assert [e["ref"] for e in report] == ["tai42/up", "tai42/current", "tai42/stuck", "tai42/broken"]
    assert by_ref["tai42/up"]["outcome"] == "upgraded"
    assert by_ref["tai42/up"]["detail"] == "1.0.0 -> 1.2.0"
    assert by_ref["tai42/current"]["outcome"] == "up-to-date"
    # The newer-but-incompatible version is NAMED, never silently omitted.
    assert "9.0.0" in by_ref["tai42/current"]["detail"]
    assert by_ref["tai42/stuck"]["outcome"] == "no-compatible-version"
    assert "2.0.0" in by_ref["tai42/stuck"]["detail"]
    assert by_ref["tai42/broken"]["outcome"] == "failed"
    assert "registry served garbage" in by_ref["tai42/broken"]["detail"]
    # The update flow resolved the PICKED pin explicitly (latest compatible).
    assert h.registry.resolve_calls == [("tai42", "up", "1.2.0")]
    # The upgraded ref's attribution row was re-stamped with the running cores.
    assert h.store.record_calls[-1][0] == "tai42/up"
    assert h.store.record_calls[-1][8:10] == ("0.1.0", "0.1.0")


async def test_upgrade_all_holds_one_lock_across_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    a = make_spec(name="a", package="pkg-a")
    b = make_spec(name="b", package="pkg-b")
    h = Harness()
    h.store.preload(a, version="1.0.0")
    h.store.preload(b, version="1.0.0")
    h.registry.versions_map = {
        "tai42/a": [_published_row("1.0.0")],
        "tai42/b": [_published_row("1.0.0")],
    }

    report = await h.installer().upgrade_all()

    assert [entry["outcome"] for entry in report] == ["up-to-date", "up-to-date"]
    # ONE acquire/release pair wraps the whole batch — never per ref.
    assert h.events.count("lock:acquire") == 1
    assert h.events.count("lock:release") == 1
    assert h.events[0] == "lock:acquire"
    assert h.events[-1] == "lock:release"


async def test_upgrade_all_lock_held_elsewhere_refuses_before_any_read() -> None:
    h = Harness()
    h.fleet.held = True
    with pytest.raises(OperationInProgressError):
        await h.installer().upgrade_all()
    assert h.events == []  # no store/registry call behind a refused lock


async def test_upgrade_all_empty_store_is_an_empty_report() -> None:
    h = Harness()
    assert await h.installer().upgrade_all() == []
