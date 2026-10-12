"""The reserved settings-profile names and their two served documents."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.schema_export import build_document_schemas
from tai42_contract.schema_export.registry import SERVED_DOCUMENTS
from tai42_contract.settings_profiles import (
    PREVIOUS_PROFILE_NAME,
    RESERVED_PROFILE_PREFIX,
    PreviousProfileName,
    SettingsProfileName,
)


def test_the_reserved_prefix_and_the_rollback_snapshot_name():
    assert RESERVED_PROFILE_PREFIX == "@"
    assert PREVIOUS_PROFILE_NAME == "@previous"
    assert PREVIOUS_PROFILE_NAME.startswith(RESERVED_PROFILE_PREFIX)


@pytest.mark.parametrize("name", ["staging", "a", "prod-eu", "x@y"])
def test_a_user_profile_name_is_any_non_empty_name_not_starting_with_the_reserved_prefix(name: str):
    assert SettingsProfileName.model_validate(name).root == name


@pytest.mark.parametrize("name", ["", "@previous", "@mine"])
def test_a_user_profile_name_refuses_the_empty_name_and_the_reserved_prefix(name: str):
    with pytest.raises(ValidationError):
        SettingsProfileName.model_validate(name)


def test_the_previous_profile_name_is_only_the_rollback_snapshot():
    assert PreviousProfileName.model_validate("@previous").root == PREVIOUS_PROFILE_NAME
    with pytest.raises(ValidationError):
        PreviousProfileName.model_validate("@other")


def test_both_names_are_served_documents():
    assert SERVED_DOCUMENTS["SettingsProfileName"] is SettingsProfileName
    assert SERVED_DOCUMENTS["PreviousProfileName"] is PreviousProfileName
    documents = build_document_schemas()["documents"]
    assert documents["SettingsProfileName"]["type"] == "string"
    assert documents["SettingsProfileName"]["minLength"] == 1
    assert documents["SettingsProfileName"]["pattern"] == "^[^@]"
    assert documents["PreviousProfileName"]["const"] == "@previous"
