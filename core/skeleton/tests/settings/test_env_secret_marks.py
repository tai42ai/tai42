"""The DERIVED masked-key set (``effective_secret_keys``).

Secret-ness of a connector's client secret is an invariant the manifest STATES, so it is
derived at read time — the stored operator marks (``TAI_ENV_SECRET_KEYS``) UNIONED with
every live ``connectors[*].client_secret_env`` — never duplicated into the env store.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.settings.env_secret_marks import effective_secret_keys, env_secret_marks_settings


@pytest.fixture(autouse=True)
def _clear_cache():
    env_secret_marks_settings.cache_clear()
    yield
    env_secret_marks_settings.cache_clear()


def _manifest_with(*connectors: dict) -> dict:
    return {"connectors": list(connectors)}


def _oauth(provider_id: str) -> dict:
    return {
        "id": provider_id,
        "kind": "oauth",
        "client_id_env": f"{provider_id.upper()}_CLIENT_ID",
        "client_secret_env": f"{provider_id.upper()}_CLIENT_SECRET",
    }


def test_unions_stored_marks_with_connector_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_ENV_SECRET_KEYS", "OPERATOR_MARK")
    env_secret_marks_settings.cache_clear()
    keys = effective_secret_keys(_manifest_with(_oauth("acme")))
    assert keys == ("ACME_CLIENT_SECRET", "OPERATOR_MARK")


def test_connector_secret_masked_with_no_stored_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_ENV_SECRET_KEYS", raising=False)
    env_secret_marks_settings.cache_clear()
    keys = effective_secret_keys(_manifest_with(_oauth("acme")))
    # Derived purely from the manifest — the client secret is masked with NO operator mark.
    assert keys == ("ACME_CLIENT_SECRET",)


def test_client_id_env_is_not_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_ENV_SECRET_KEYS", raising=False)
    env_secret_marks_settings.cache_clear()
    keys = effective_secret_keys(_manifest_with(_oauth("acme")))
    assert "ACME_CLIENT_ID" not in keys


def test_none_connector_contributes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_ENV_SECRET_KEYS", raising=False)
    env_secret_marks_settings.cache_clear()
    keys = effective_secret_keys(_manifest_with({"id": "iota", "kind": "none"}))
    assert keys == ()


def test_missing_connectors_key_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_ENV_SECRET_KEYS", "OPERATOR_MARK")
    env_secret_marks_settings.cache_clear()
    keys = effective_secret_keys({})  # no 'connectors' key
    assert keys == ("OPERATOR_MARK",)


def test_deduped_and_sorted(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stored mark equal to a connector secret name collapses; the result is sorted.
    monkeypatch.setenv("TAI_ENV_SECRET_KEYS", "ZED_MARK,ACME_CLIENT_SECRET")
    env_secret_marks_settings.cache_clear()
    keys = effective_secret_keys(_manifest_with(_oauth("acme"), _oauth("iota")))
    assert keys == ("ACME_CLIENT_SECRET", "IOTA_CLIENT_SECRET", "ZED_MARK")


# ---------------------------------------------------------------------------
# the marks variable's parse / format / merge helpers
# ---------------------------------------------------------------------------


def test_marks_variable_name() -> None:
    from tai42_skeleton.settings.env_secret_marks import SECRET_MARKS_ENV_VAR

    assert SECRET_MARKS_ENV_VAR == "TAI_ENV_SECRET_KEYS"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        (" , ,", []),
        ("A", ["A"]),
        (" B , A ,, B ", ["B", "A"]),
    ],
)
def test_parse_secret_marks_trims_drops_empties_and_dedupes_in_order(raw: str | None, expected: list[str]) -> None:
    from tai42_skeleton.settings.env_secret_marks import parse_secret_marks

    assert parse_secret_marks(raw) == expected


def test_format_secret_marks_joins_with_commas() -> None:
    from tai42_skeleton.settings.env_secret_marks import format_secret_marks

    assert format_secret_marks(["B", "A"]) == "B,A"
    assert format_secret_marks([]) == ""


def test_merge_secret_marks_is_an_ordered_union() -> None:
    from tai42_skeleton.settings.env_secret_marks import merge_secret_marks

    assert merge_secret_marks("B, A", ["A", "C", "C"]) == "B,A,C"
    assert merge_secret_marks(None, ["X"]) == "X"
    assert merge_secret_marks("X", []) == "X"


def test_the_settings_group_parses_through_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.settings.env_secret_marks import EnvSecretMarksSettings

    monkeypatch.setenv("TAI_ENV_SECRET_KEYS", " B , A ,, B ")
    assert EnvSecretMarksSettings().secret_keys == ["B", "A"]
