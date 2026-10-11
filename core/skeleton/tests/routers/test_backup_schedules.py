"""The ``schedules`` backup section, driven through the backup router.

The section carries the scheduling backend's ``backend_export_schedules`` rows validated
as the contract's ``ScheduleRecord`` and reads the ``backend_import_schedules`` result as
a ``ScheduleImportReport``. The ``tai42_app.tools`` facet is faked: an unbound backend has
neither tool, so ``run_tool`` raises the binding's real unknown-tool error and the router
records it per-section; a bound backend round-trips the records.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.backup.registry import BackupRegistry
from tai42_skeleton.backup.sections import register_core_sections
from tai42_skeleton.routers.backup import export_backup, import_backup
from tai42_skeleton.tools.binding import UnknownToolError


def _post_req(payload: dict) -> Request:
    body = json.dumps(payload).encode()
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""}
    delivered = {"done": False}

    async def receive():
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _json(resp) -> dict:
    return json.loads(bytes(resp.body))


class _FakeTools:
    """A tool registry where ``registered`` names run and return their queued
    result; any other name raises the binding's unknown-tool ``RuntimeError``."""

    def __init__(self, registered: dict[str, object] | None = None) -> None:
        self._registered = registered or {}
        self.run_calls: list[tuple[str, dict]] = []

    async def run_tool(self, key, arguments):
        if key not in self._registered:
            raise UnknownToolError(key)
        self.run_calls.append((key, arguments))
        return self._registered[key]


def _install(monkeypatch, tools: _FakeTools) -> None:
    registry = BackupRegistry()
    register_core_sections(registry)
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(backup=registry, tools=tools))


_EMPTY_REPORT = {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}


def _record(name: str, tool: str, kwargs: dict) -> dict:
    return {
        "name": name,
        "tool": tool,
        "args": [],
        "kwargs": kwargs,
        "cadence": {"kind": "interval", "every": 60.0, "relative": False},
        "enabled": True,
    }


# -- no backend: absence is a per-section error, not a crash -----------------


async def test_export_without_backend_records_section_error(monkeypatch):
    _install(monkeypatch, _FakeTools())  # no scheduling tools registered
    resp = await export_backup(_post_req({"sections": ["schedules"]}))
    assert resp.status_code == 200  # still a download, never a 500
    doc = _json(resp)
    assert "schedules" not in doc["sections"]  # nothing exported
    assert "backend_export_schedules" in doc["errors"]["schedules"]


async def test_import_without_backend_reports_error_and_not_ok(monkeypatch):
    _install(monkeypatch, _FakeTools())
    document = {"version": 1, "sections": {"schedules": [{"name": "nightly"}]}}
    data = _json(await import_backup(_post_req({"document": document, "sections": ["schedules"]})))["data"]
    assert data["ok"] is False
    assert "backend_import_schedules" in data["sections"]["schedules"]["errors"][0]


# -- bound backend: the document round-trips opaquely ------------------------


async def test_schedules_round_trip_through_bound_backend(monkeypatch):
    backend_doc = [_record("nightly", "cleanup", {"scope": "all"}), _record("hourly", "sync", {})]
    import_report = {"created": 2, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}
    tools = _FakeTools(
        {
            "backend_export_schedules": backend_doc,
            "backend_import_schedules": import_report,
        }
    )
    _install(monkeypatch, tools)

    # Export carries the backend's records, validated.
    doc = _json(await export_backup(_post_req({"sections": ["schedules"]})))
    assert doc["sections"]["schedules"] == backend_doc
    assert doc["errors"] == {}

    # Import hands the whole document back to the backend under ``schedules``, forwarding
    # the per-record mode across the tool boundary (skip is the default). The backend's
    # report maps into the typed section report: its counts fill the typed fields, and its
    # ``skipped_existing`` tally rides ``details``.
    data = _json(await import_backup(_post_req({"document": doc, "sections": ["schedules"]})))["data"]
    assert data["ok"] is True
    assert data["sections"]["schedules"] == {
        "created": 2,
        "updated": 0,
        "skipped": 0,
        "errors": [],
        "details": {"skipped_existing": 0},
        "fanout": None,
        "recycle": None,
        "awaiting_respawn": [],
    }
    assert ("backend_import_schedules", {"schedules": backend_doc, "mode": "skip"}) in tools.run_calls


async def test_schedules_backend_structured_errors_render_as_strings(monkeypatch):
    # A backend reports a per-row failure as ``{"index", "name", "error"}``; the section
    # maps each to a string for the typed report (list[str]), so a landed restore that a
    # backend partly rejected is reported truthfully rather than failing the typed shape.
    backend_doc = [{"name": "nightly", "cron": "bad"}]
    import_report = {
        "created": 0,
        "updated": 0,
        "skipped": 1,
        "skipped_existing": 0,
        "errors": [{"index": 0, "name": "nightly", "error": "unsupported schedule"}],
    }
    tools = _FakeTools({"backend_import_schedules": import_report})
    _install(monkeypatch, tools)

    document = {"version": 1, "sections": {"schedules": backend_doc}}
    data = _json(await import_backup(_post_req({"document": document, "sections": ["schedules"]})))["data"]
    assert data["ok"] is False
    section = data["sections"]["schedules"]
    assert section["skipped"] == 1
    assert section["errors"] == ["schedule 'nightly' (row 0): unsupported schedule"]


async def test_schedules_import_forwards_overwrite_mode(monkeypatch):
    backend_doc = [_record("nightly", "cleanup", {})]
    import_report = {"created": 0, "updated": 1, "skipped": 0, "skipped_existing": 0, "errors": []}
    tools = _FakeTools({"backend_import_schedules": import_report})
    _install(monkeypatch, tools)

    document = {"version": 1, "sections": {"schedules": backend_doc}}
    data = _json(
        await import_backup(_post_req({"document": document, "sections": ["schedules"], "mode": "overwrite"}))
    )["data"]
    assert data["ok"] is True
    assert ("backend_import_schedules", {"schedules": backend_doc, "mode": "overwrite"}) in tools.run_calls


# -- the schedule definition check on restore -----------------------------------------------------


def _bound_row(name: str, state: str) -> dict:
    from tai42_kit.utils.schedule_subject import SCHEDULE_STATE_BINDING_ARG

    binding = {"states": [{"state": state, "subject_expr": {"content": ".x"}, "templates": ["t1"]}]}
    return {"name": name, "kwargs": {"tool": "cleanup", SCHEDULE_STATE_BINDING_ARG: binding}}


def _patch_binding_validation(monkeypatch, error_for=None) -> list:
    from tai42_skeleton.tools import state_binding as state_binding_module

    calls: list = []

    async def _validate_and_attach(app, binding) -> None:
        calls.append(binding)
        error = error_for(binding) if error_for is not None else None
        if error is not None:
            raise error

    monkeypatch.setattr(state_binding_module, "validate_and_attach_binding", _validate_and_attach)
    return calls


async def test_a_refused_binding_row_is_not_forwarded_to_the_backend_and_is_reported(monkeypatch):
    from tai42_contract.states.errors import StateNotFoundError

    calls = _patch_binding_validation(
        monkeypatch,
        lambda b: StateNotFoundError("state 'ghost' is not declared") if b.states[0].state == "ghost" else None,
    )
    bad, good, plain = _bound_row("bad", "ghost"), _bound_row("good", "status"), {"name": "plain", "kwargs": {}}
    import_report = {
        "created": 1,
        "updated": 0,
        "skipped": 1,
        "skipped_existing": 0,
        # The backend saw [good, plain]: its row 1 is the payload's row 2.
        "errors": [{"index": 1, "name": "plain", "error": "unsupported schedule"}],
    }
    tools = _FakeTools({"backend_import_schedules": import_report})
    _install(monkeypatch, tools)

    document = {"version": 1, "sections": {"schedules": [bad, good, plain]}}
    data = _json(await import_backup(_post_req({"document": document, "sections": ["schedules"]})))["data"]

    assert tools.run_calls == [("backend_import_schedules", {"schedules": [good, plain], "mode": "skip"})]
    section = data["sections"]["schedules"]
    assert section["errors"] == [
        "schedule 'bad' (row 0): state 'ghost' is not declared",
        "schedule 'plain' (row 2): unsupported schedule",
    ]
    assert section["skipped"] == 2
    assert section["created"] == 1
    assert [binding.states[0].state for binding in calls] == ["ghost", "status"]


async def test_a_malformed_binding_row_is_refused_per_row(monkeypatch):
    from tai42_kit.utils.schedule_subject import SCHEDULE_STATE_BINDING_ARG

    _patch_binding_validation(monkeypatch)
    tools = _FakeTools({"backend_import_schedules": _EMPTY_REPORT})
    _install(monkeypatch, tools)
    malformed = {"name": "bad", "kwargs": {SCHEDULE_STATE_BINDING_ARG: {"states": "not-a-list"}}}

    document = {"version": 1, "sections": {"schedules": [malformed]}}
    data = _json(await import_backup(_post_req({"document": document, "sections": ["schedules"]})))["data"]

    section = data["sections"]["schedules"]
    assert section["skipped"] == 1
    assert section["errors"][0].startswith("schedule 'bad' (row 0): ")
    assert tools.run_calls == [("backend_import_schedules", {"schedules": [], "mode": "skip"})]


async def test_a_store_fault_during_the_binding_check_fails_the_section(monkeypatch):
    from tai42_contract.states.errors import StatesError, StatesNotConfiguredError

    for fault in (StatesError("store down"), StatesNotConfiguredError("store down")):
        _patch_binding_validation(monkeypatch, lambda b, fault=fault: fault)
        tools = _FakeTools({"backend_import_schedules": _EMPTY_REPORT})
        _install(monkeypatch, tools)

        document = {"version": 1, "sections": {"schedules": [_bound_row("nightly", "status")]}}
        data = _json(await import_backup(_post_req({"document": document, "sections": ["schedules"]})))["data"]

        assert data["ok"] is False
        assert data["sections"]["schedules"]["errors"] == ["store down"]
        assert data["sections"]["schedules"]["skipped"] == 0
        assert tools.run_calls == []


async def test_a_document_that_is_not_a_row_list_is_forwarded_to_the_backend_unchanged(monkeypatch):
    # Only a row list carries bindings the platform can read; any other document is the backend's
    # to judge, forwarded as it is.
    calls = _patch_binding_validation(monkeypatch)
    tools = _FakeTools({"backend_import_schedules": _EMPTY_REPORT})
    _install(monkeypatch, tools)
    opaque = {"format": "backend-native", "rows": [_bound_row("nightly", "status")]}

    document = {"version": 1, "sections": {"schedules": opaque}}
    data = _json(await import_backup(_post_req({"document": document, "sections": ["schedules"]})))["data"]

    assert data["ok"] is True
    assert tools.run_calls == [("backend_import_schedules", {"schedules": opaque, "mode": "skip"})]
    assert calls == []


async def test_an_export_that_is_not_schedule_records_is_a_section_error(monkeypatch):
    legacy = [{"name": "nightly", "kwargs": {}, "schedule": {"__type__": "interval", "every": 60}, "enabled": True}]
    _install(monkeypatch, _FakeTools({"backend_export_schedules": legacy}))
    doc = _json(await export_backup(_post_req({"sections": ["schedules"]})))
    assert "schedules" not in doc["sections"]
    assert "ScheduleRecord" in doc["errors"]["schedules"]


async def test_a_report_that_is_not_a_schedule_import_report_fails_the_section(monkeypatch):
    # A report missing ``skipped_existing`` is not the contract's shape: the section errors loudly
    # instead of filling zero defaults.
    tools = _FakeTools({"backend_import_schedules": {"created": 1, "updated": 0, "skipped": 0, "errors": []}})
    _install(monkeypatch, tools)
    document = {"version": 1, "sections": {"schedules": [_record("nightly", "cleanup", {})]}}
    data = _json(await import_backup(_post_req({"document": document, "sections": ["schedules"]})))["data"]
    assert data["ok"] is False
    (error,) = data["sections"]["schedules"]["errors"]
    assert (
        "the scheduling backend's backend_import_schedules returned a report that is not ScheduleImportReport" in error
    )
