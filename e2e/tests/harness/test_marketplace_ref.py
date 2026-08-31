"""The dispatched-marketplace-ref resolver and its dual-cause loud-fail.

Pure unit tests (no infra, no booted registry): they monkeypatch the env and the
registry-venv probe to prove :func:`_marketplace_ref` honors a dispatched
``TAI_E2E_MARKETPLACE_REF`` (validating its shape loudly), and that a False
declared-routes gate under a dispatched ref escalates to a FAIL — distinguishing a
harness ordering bug (registry venv absent) from a contract-floor regression — while
an UNdispatched run still skips, byte-for-byte the prior behavior.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from tai42_e2e import marketplace
from tai42_e2e.marketplace import (
    _MARKETPLACE_PIN,
    _MARKETPLACE_REF_ENV,
    _marketplace_ref,
    declared_routes_dispatch_failure,
)

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


# ---- declared_routes_dispatch_failure dual-cause ------------------------


def test_no_dispatch_returns_none_so_caller_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_MARKETPLACE_REF_ENV, raising=False)
    assert declared_routes_dispatch_failure() is None


def test_dispatched_but_venv_absent_reports_ordering_bug(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(_MARKETPLACE_REF_ENV, _A_SHA)
    monkeypatch.setattr(marketplace, "_registry_python", lambda: tmp_path / "does-not-exist" / "python")
    detail = declared_routes_dispatch_failure()
    assert detail is not None
    assert "ordering bug" in detail
    assert _A_SHA in detail


def test_dispatched_with_built_venv_reports_contract_floor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    present = tmp_path / "python"
    present.write_text("", encoding="utf-8")
    monkeypatch.setenv(_MARKETPLACE_REF_ENV, _A_SHA)
    monkeypatch.setattr(marketplace, "_registry_python", lambda: present)
    detail = declared_routes_dispatch_failure()
    assert detail is not None
    assert "regressed its contract floor" in detail
    assert _A_SHA in detail


# ---- skip_unless_registry_supports_declared_routes ----------------------
#
# The skip helper lives with the marketplace specs (collected only under
# TAI_E2E_MARKETPLACE=1), but importing the module and calling the helper needs no
# stack, so its skip-vs-fail branching is unit-tested here.


def test_skip_helper_noop_when_gate_true(monkeypatch: pytest.MonkeyPatch) -> None:
    support = _support()
    monkeypatch.setattr(support, "registry_supports_declared_routes", lambda: True)
    support.skip_unless_registry_supports_declared_routes()  # must not raise


def test_skip_helper_skips_when_gate_false_and_undispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    support = _support()
    monkeypatch.delenv(_MARKETPLACE_REF_ENV, raising=False)
    monkeypatch.setattr(support, "registry_supports_declared_routes", lambda: False)
    with pytest.raises(pytest.skip.Exception):
        support.skip_unless_registry_supports_declared_routes()


def test_skip_helper_fails_when_gate_false_and_dispatched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    support = _support()
    monkeypatch.setenv(_MARKETPLACE_REF_ENV, _A_SHA)
    monkeypatch.setattr(support, "registry_supports_declared_routes", lambda: False)
    monkeypatch.setattr(marketplace, "_registry_python", lambda: tmp_path / "absent" / "python")
    with pytest.raises(pytest.fail.Exception) as exc:
        support.skip_unless_registry_supports_declared_routes()
    assert "ordering bug" in str(exc.value)


def _support() -> ModuleType:
    """Import the marketplace specs' shared support module by file path (it lives
    under ``tests/marketplace`` which is not an importable package)."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "marketplace" / "_market_support.py"
    spec = importlib.util.spec_from_file_location("_e2e_market_support_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
