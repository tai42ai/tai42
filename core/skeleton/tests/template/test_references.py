"""The stored-template check: each referenced id is a canonical key the storage provider holds."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.tools import TemplateRef

from tai42_skeleton.app import instance
from tai42_skeleton.template.references import assert_templates_stored, template_reference_faults

from .._fakes.recording_storage import RecordingStorage


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RecordingStorage:
    provider = RecordingStorage({"schemas/out": (b"{}", "application/json")})
    monkeypatch.setattr(instance.app._storage_registry, "_provider", provider)
    return provider


async def test_a_stored_id_answers_no_line(store: RecordingStorage) -> None:
    assert await template_reference_faults([TemplateRef(id="schemas/out", site="output_schema")]) == []


async def test_a_missing_id_answers_the_not_stored_line(store: RecordingStorage) -> None:
    assert await template_reference_faults([TemplateRef(id="schemas/missing", site="output_schema")]) == [
        "stored id 'schemas/missing' at output_schema is not a stored template"
    ]


async def test_a_non_canonical_id_answers_the_guard_line_without_a_stat(store: RecordingStorage) -> None:
    assert await template_reference_faults([TemplateRef(id="./x", site="output_schema")]) == [
        "stored id './x' at output_schema: non-canonical store key './x': "
        "a key is '/'-separated names with no empty, '.' or '..' segment"
    ]
    assert store.calls == []


async def test_no_provider_answers_the_no_provider_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instance.app._storage_registry, "_provider", None)
    assert await template_reference_faults([TemplateRef(id="schemas/out", site="output_schema")]) == [
        "stored id 'schemas/out' at output_schema is not a stored template: no storage provider is installed"
    ]


async def test_one_id_at_two_sites_is_two_lines_and_one_stat(store: RecordingStorage) -> None:
    refs = [
        TemplateRef(id="jq/missing", site="states[0].subject_expr"),
        TemplateRef(id="schemas/out", site="output_schema"),
        TemplateRef(id="jq/missing", site="states[0].scope_expr"),
        TemplateRef(id="jq/missing", site="states[0].subject_expr"),
    ]
    assert await template_reference_faults(refs) == [
        "stored id 'jq/missing' at states[0].subject_expr is not a stored template",
        "stored id 'jq/missing' at states[0].scope_expr is not a stored template",
    ]
    assert store.calls == [("stat", "jq/missing"), ("stat", "schemas/out")]


async def test_a_locale_variant_alone_does_not_store_the_bare_id(store: RecordingStorage) -> None:
    store.put("prompts/hello@he", b"shalom")
    assert await template_reference_faults([TemplateRef(id="prompts/hello", site="system_prompt")]) == [
        "stored id 'prompts/hello' at system_prompt is not a stored template"
    ]


async def test_any_other_provider_failure_propagates(store: RecordingStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    failure = OSError("store unreachable")

    async def _fail(path: str) -> None:
        raise failure

    monkeypatch.setattr(store, "stat", _fail)
    with pytest.raises(OSError, match=r"^store unreachable$") as caught:
        await template_reference_faults([TemplateRef(id="schemas/out", site="output_schema")])
    assert caught.value is failure


async def test_assert_raises_the_callers_error_with_every_line(store: RecordingStorage) -> None:
    refs = [TemplateRef(id="a/missing", site="schema"), TemplateRef(id="b/missing", site="declarations.check")]
    with pytest.raises(LookupError) as caught:
        await assert_templates_stored(refs, LookupError)
    assert str(caught.value) == (
        "stored id 'a/missing' at schema is not a stored template\n"
        "stored id 'b/missing' at declarations.check is not a stored template"
    )


async def test_assert_is_silent_when_every_id_is_stored(store: RecordingStorage) -> None:
    await assert_templates_stored([TemplateRef(id="schemas/out", site="output_schema")], LookupError)


async def test_the_distinct_ids_are_checked_concurrently(
    store: RecordingStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each stat waits until every distinct id's stat is in flight: a check that asks one id
    # after another never gets there and times out.
    ids = [f"jq/t{i}" for i in range(5)]
    in_flight: set[str] = set()
    all_in_flight = asyncio.Event()

    async def _stat(path: str) -> None:
        in_flight.add(path)
        if in_flight == set(ids):
            all_in_flight.set()
        await asyncio.wait_for(all_in_flight.wait(), timeout=2)
        raise FileNotFoundError(path)

    monkeypatch.setattr(store, "stat", _stat)
    refs = [TemplateRef(id=resource_id, site="system_prompt") for resource_id in [*ids, ids[0]]]
    assert await template_reference_faults(refs) == [
        f"stored id {resource_id!r} at system_prompt is not a stored template" for resource_id in ids
    ]


async def test_a_provider_failure_cancels_the_checks_still_in_flight(
    store: RecordingStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError("store unreachable")
    cancelled: list[str] = []

    async def _stat(path: str) -> None:
        if path == "jq/fails":
            raise failure
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(path)
            raise

    monkeypatch.setattr(store, "stat", _stat)
    refs = [TemplateRef(id=resource_id, site="system_prompt") for resource_id in ("jq/slow", "jq/fails")]
    with pytest.raises(OSError, match=r"^store unreachable$") as caught:
        await asyncio.wait_for(template_reference_faults(refs), timeout=2)
    assert caught.value is failure
    assert cancelled == ["jq/slow"]


async def test_failures_that_land_together_propagate_as_their_group(
    store: RecordingStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _stat(path: str) -> None:
        raise OSError(f"{path} unreachable")

    monkeypatch.setattr(store, "stat", _stat)
    refs = [TemplateRef(id=resource_id, site="system_prompt") for resource_id in ("jq/a", "jq/b")]
    with pytest.raises(ExceptionGroup) as caught:
        await template_reference_faults(refs)
    assert sorted(str(error) for error in caught.value.exceptions) == ["jq/a unreachable", "jq/b unreachable"]
