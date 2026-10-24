"""The dispatched-marketplace-ref resolver.

Pure unit tests (no infra, no booted registry): they monkeypatch the env to prove
:func:`_marketplace_ref` honors a dispatched ``TAI_E2E_MARKETPLACE_REF`` (validating
its shape loudly) and that the dedicated registry venv is keyed by the resolved ref.
"""

from __future__ import annotations

import pytest

from tai42_e2e import marketplace
from tai42_e2e.marketplace import (
    _MARKETPLACE_PIN,
    _MARKETPLACE_REF_ENV,
    _marketplace_ref,
)

pytestmark = pytest.mark.needs("no-stack")

_A_SHA = "0123456789abcdef0123456789abcdef01234567"


# ---- _marketplace_ref resolver ------------------------------------------


def test_ref_unset_resolves_the_checked_in_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_MARKETPLACE_REF_ENV, raising=False)
    assert _marketplace_ref() == _MARKETPLACE_PIN


def test_ref_valid_sha_resolves_that_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MARKETPLACE_REF_ENV, _A_SHA)
    assert _marketplace_ref() == _A_SHA


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-sha",
        "abc123",  # too short
        _A_SHA + "0",  # too long
        _A_SHA.upper(),  # uppercase hex rejected (lowercase only)
        "main",
    ],
)
def test_ref_invalid_value_raises_naming_env_and_shape(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(_MARKETPLACE_REF_ENV, bad)
    with pytest.raises(RuntimeError) as exc:
        _marketplace_ref()
    msg = str(exc.value)
    assert _MARKETPLACE_REF_ENV in msg
    assert "40" in msg  # names the expected 40-char hex shape


def test_venv_dir_is_keyed_by_the_dispatched_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MARKETPLACE_REF_ENV, _A_SHA)
    assert marketplace._registry_venv_dir().name.endswith(_A_SHA[:12])
