"""The per-kind restore checks of the versioned-document restore, proven with a neutral kind.

A kind registers a check over its document's ACTIVE version body; the restore runs it before
writing the document: a refusal skips the document and its version rows and is reported, any
other error fails the section. The registry is a process registry keyed by kind; the tests
register into it through ``monkeypatch`` so nothing leaks between tests.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from tai42_contract.states.errors import StateNotFoundError, StatesError, StatesNotConfiguredError
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.versioning.backup as versioning_backup
from tai42_skeleton.versioning import restore_checks
from tai42_skeleton.versioning.backup import import_versioned_documents
from tai42_skeleton.versioning.restore_checks import register_restore_check, restore_check

from .test_backup import _FakeVersioningBackupPg

_KIND = "probe-doc"
_T0 = "2024-01-01T00:00:00+00:00"


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch) -> _FakeVersioningBackupPg:
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    fake = _FakeVersioningBackupPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield fake

    monkeypatch.setattr(versioning_backup, "client_ctx", fake_client_ctx)
    return fake


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Register a probe check for ``probe-doc``: it records each call and refuses a body naming ``refuse``."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _check(name: str, body: dict[str, Any]) -> None:
        calls.append((name, body))
        refusal = body.get("refuse")
        if refusal == "value":
            raise ValueError("the body is refused")
        if refusal == "state":
            raise StateNotFoundError("state 'ghost' is not declared")
        if refusal == "fault":
            raise StatesError("store down")
        if refusal == "unbound":
            raise StatesNotConfiguredError("store unbound")
        if refusal == "transport":
            raise ConnectionError("database down")

    monkeypatch.setitem(restore_checks._checks, _KIND, _check)
    return calls


def _document(doc_id: int, name: str, *, active: int = 1, is_active: bool = True, kind: str = _KIND) -> dict:
    return {
        "id": doc_id,
        "kind": kind,
        "name": name,
        "active_version": active,
        "is_active": is_active,
        "created_at": _T0,
    }


def _version(version_id: int, doc_id: int, version: int, body: dict) -> dict:
    return {
        "id": version_id,
        "document_id": doc_id,
        "version": version,
        "body": body,
        "tags": [],
        "created_at": _T0,
    }


def test_a_registered_check_is_served_by_its_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restore_checks, "_checks", {})

    async def _check(name: str, body: dict[str, Any]) -> None:
        return None

    register_restore_check(_KIND, _check)
    assert restore_check(_KIND) is _check
    assert restore_check("other-kind") is None


def test_a_second_check_for_one_kind_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restore_checks, "_checks", {})

    async def _check(name: str, body: dict[str, Any]) -> None:
        return None

    register_restore_check(_KIND, _check)
    with pytest.raises(ValueError, match="already registered"):
        register_restore_check(_KIND, _check)


async def test_the_check_runs_over_the_active_version_body_with_the_document_name(pg, seen) -> None:
    payload = {
        "documents": [_document(1, "doc-a", active=2)],
        "versions": [_version(1, 1, 1, {"n": 1}), _version(2, 1, 2, {"n": 2})],
    }
    report = await import_versioned_documents(payload)
    assert report.errors == []
    assert report.created == 1
    assert seen == [("doc-a", {"n": 2})]
    assert len(pg.versions) == 2


@pytest.mark.parametrize(
    ("refusal", "message"),
    [("value", "the body is refused"), ("state", "state 'ghost' is not declared")],
)
async def test_a_refused_document_is_skipped_with_its_versions_and_reported(pg, seen, refusal, message) -> None:
    payload = {
        "documents": [_document(1, "doc-a"), _document(2, "doc-b")],
        "versions": [_version(1, 1, 1, {"refuse": refusal}), _version(2, 2, 1, {"n": 1})],
    }
    report = await import_versioned_documents(payload)
    assert report.errors == [f"probe-doc 'doc-a': {message}"]
    assert report.skipped == 1
    assert report.created == 1
    assert [d["name"] for d in pg.documents] == ["doc-b"]
    assert [v["document_id"] for v in pg.versions] == [2]


@pytest.mark.parametrize("refusal", ["fault", "unbound", "transport"])
async def test_any_other_error_fails_the_section(pg, seen, refusal) -> None:
    payload = {"documents": [_document(1, "doc-a")], "versions": [_version(1, 1, 1, {"refuse": refusal})]}
    with pytest.raises((StatesError, ConnectionError)):
        await import_versioned_documents(payload)


async def test_a_kind_with_no_check_restores_unchecked(pg, seen) -> None:
    payload = {
        "documents": [_document(1, "other", kind="other-kind")],
        "versions": [_version(1, 1, 1, {"refuse": "value"})],
    }
    report = await import_versioned_documents(payload)
    assert report.errors == []
    assert report.created == 1
    assert seen == []


async def test_a_soft_deleted_document_restores_as_history_unchecked(pg, seen) -> None:
    # A soft-deleted document is never live, so its body is restored as history with no save rule.
    payload = {
        "documents": [_document(1, "doc-a", is_active=False)],
        "versions": [_version(1, 1, 1, {"refuse": "value"})],
    }
    report = await import_versioned_documents(payload)
    assert report.errors == []
    assert report.created == 1
    assert seen == []


async def test_an_active_document_whose_active_version_is_absent_is_refused(pg, seen) -> None:
    payload = {"documents": [_document(1, "doc-a", active=3)], "versions": [_version(1, 1, 1, {"n": 1})]}
    report = await import_versioned_documents(payload)
    assert report.errors == ["probe-doc 'doc-a': its active version 3 is not in the backup"]
    assert report.skipped == 1
    assert pg.documents == []
    assert pg.versions == []
    assert seen == []


async def test_an_existing_document_under_skip_is_left_unchecked(pg, seen) -> None:
    pg.documents.append(_document(1, "doc-a") | {"created_at": datetime(2024, 1, 1, tzinfo=UTC)})
    payload = {"documents": [_document(1, "doc-a")], "versions": [_version(1, 1, 1, {"refuse": "value"})]}
    report = await import_versioned_documents(payload, "skip")
    assert report.details["skipped_existing"] == 1
    assert report.errors == []
    assert seen == []


async def test_an_existing_document_under_overwrite_is_checked(pg, seen) -> None:
    pg.documents.append(_document(1, "doc-a") | {"created_at": datetime(2024, 1, 1, tzinfo=UTC)})
    payload = {"documents": [_document(1, "doc-a")], "versions": [_version(1, 1, 1, {"refuse": "value"})]}
    report = await import_versioned_documents(payload, "overwrite")
    assert report.errors == ["probe-doc 'doc-a': the body is refused"]
    assert report.updated == 0
    assert report.skipped == 1


def test_presets_register_their_restore_check() -> None:
    import tai42_skeleton.presets  # noqa: F401  (the presets package registers its kind's check)

    assert restore_check("preset") is not None
