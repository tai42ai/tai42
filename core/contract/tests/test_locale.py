"""The one canonical form + validator for a subject's BCP 47 locale, and the carriers
(:class:`Person`, :class:`SubjectCandidates`) that store it canonically or explicitly absent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tai42_contract.conversations import Person, PersonAddress
from tai42_contract.locale import InvalidLocaleError, canonical_locale, normalize_optional_locale
from tai42_contract.states import SubjectCandidates


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("he-il", "he-IL"), ("EN", "en"), ("zh-hant-tw", "zh-Hant-TW"), ("pt-BR", "pt-BR"), ("  he  ", "he")],
)
def test_canonical_locale_normalizes(raw: str, expected: str) -> None:
    assert canonical_locale(raw) == expected


@pytest.mark.parametrize("bad", ["", "not a locale!", "e", "-he", "he-", "toolongsubtag9"])
def test_canonical_locale_rejects_malformed(bad: str) -> None:
    with pytest.raises(InvalidLocaleError):
        canonical_locale(bad)


def test_normalize_optional_passes_none_through() -> None:
    assert normalize_optional_locale(None) is None
    assert normalize_optional_locale("he-il") == "he-IL"


def _address() -> PersonAddress:
    return PersonAddress(door="api", routes=["r"], address="x", linked_at=datetime.now(UTC))


def _person(locale: str | None) -> Person:
    return Person(
        person_id="p",
        target_kind="tool",
        target_name="t",
        created_at=datetime.now(UTC),
        addresses=[_address()],
        locale=locale,
    )


def test_person_stores_locale_canonically_or_absent() -> None:
    assert _person(locale="He-il").locale == "he-IL"
    assert _person(locale=None).locale is None


def test_subject_candidates_stores_locale_canonically_or_absent() -> None:
    assert SubjectCandidates(target_kind="tool", target_name="t", locale="he-il").locale == "he-IL"
    assert SubjectCandidates(target_kind="tool", target_name="t").locale is None
