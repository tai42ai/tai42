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
