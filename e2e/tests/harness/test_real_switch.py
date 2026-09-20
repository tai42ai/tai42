"""The REAL/MOCK switch engine: selection parsing, the required-creds registry,
and the loud-fail-at-collection that a real leg without its credentials must hit.

The switch itself is proven inert when ``TAI_E2E_REAL`` is empty (the whole mock
suite runs under exactly that condition); these tests pin the loud behavior the
per-service real legs depend on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tai42_e2e.pytest_plugin import RealSelectionError, assert_real_selection_ready
from tai42_e2e.settings import REAL_SERVICES, HarnessSettings


def _settings(real: str, *, public_base_url: str | None = None) -> HarnessSettings:
    # Construct off explicit values, bypassing the ambient env so the assertion is
    # about the switch logic, not the host's environment.
    return HarnessSettings(real=real, public_base_url=public_base_url)  # type: ignore[call-arg]


def test_empty_selection_is_inert() -> None:
    """No service named = every seam mock. ``is_real`` is False everywhere, the
    selection sets are empty, and the loud-fail gate passes with an empty env."""
    s = _settings("")
    assert s.real_services == frozenset()
    assert s.real_inbound_services == frozenset()
    for service in REAL_SERVICES:
        assert s.is_real(service) is False
    # The gate is a no-op even with a completely empty environment.
    assert_real_selection_ready(s, {})


def test_unknown_service_raises() -> None:
    s = _settings("stripe,not-a-service")
    with pytest.raises(ValueError, match="not-a-service"):
        _ = s.real_services


def test_is_real_rejects_unknown_service() -> None:
    with pytest.raises(ValueError, match="unknown service"):
        _settings("").is_real("nope")


def test_missing_creds_named_exactly() -> None:
    """A real seam whose creds are absent is reported with its EXACT missing var
    names — the contract the loud-fail message and the real legs both read."""
    s = _settings("stripe")
    missing = s.missing_real_creds({})
    assert missing == {"stripe": ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]}
    # A present (non-empty) var drops out of the missing set; an empty one stays.
    partial = s.missing_real_creds({"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": ""})
    assert partial == {"stripe": ["STRIPE_WEBHOOK_SECRET"]}


def test_loud_fail_gate_names_missing_vars_and_service() -> None:
    s = _settings("stripe")
    with pytest.raises(RealSelectionError) as exc:
        assert_real_selection_ready(s, {})
    msg = str(exc.value)
    assert "stripe" in msg
    assert "STRIPE_SECRET_KEY" in msg
    assert "STRIPE_WEBHOOK_SECRET" in msg


def test_inbound_selection_requires_public_base_url() -> None:
    # Creds present but the inbound seam has no public origin -> still loud.
    env = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": "whsec_x"}
    with pytest.raises(RealSelectionError, match="E2E_PUBLIC_BASE_URL"):
        assert_real_selection_ready(_settings("stripe"), env)
    # With the public origin set and creds present, the gate passes.
    assert_real_selection_ready(_settings("stripe", public_base_url="https://e2e.example"), env)


def test_outbound_selection_needs_no_public_base_url() -> None:
    # A purely outbound seam (llm) with its cred present passes without a public URL.
    assert_real_selection_ready(_settings("llm"), {"OPENAI_API_KEY": "sk-x"})


def test_llm_required_cred_follows_selected_provider() -> None:
    # The llm/embeddings seams are provider-configurable: the required key is the
    # SELECTED provider's, not a hardcoded OPENAI_API_KEY.
    s = _settings("llm,embeddings")
    # default provider (openai) -> OPENAI_API_KEY for both
    assert s.missing_real_creds({}) == {"llm": ["OPENAI_API_KEY"], "embeddings": ["OPENAI_API_KEY"]}
    # a non-default llm provider names ITS key; openai key alone no longer satisfies llm
    env = {"REAL_E2E_LLM_PROVIDER": "anthropic", "OPENAI_API_KEY": "sk-x"}
    assert s.missing_real_creds(env) == {"llm": ["ANTHROPIC_API_KEY"]}
    # both keys present -> nothing missing
    full = {"REAL_E2E_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-x"}
    assert s.missing_real_creds(full) == {}


def test_storage_real_seam_requires_matching_axis() -> None:
    # storage-s3 named real but the storage axis still on the hermetic mock (default
    # 'local') -> a silent no-op the gate must catch.
    s = _settings("storage-s3")
    problems = s.storage_axis_mismatches()
    assert len(problems) == 1
    assert "TAI_E2E_STORAGE=s3-real" in problems[0]
    # With the axis paired to the real variant, no mismatch.
    paired = HarnessSettings(real="storage-s3", storage="s3-real")  # type: ignore[call-arg]
    assert paired.storage_axis_mismatches() == []
    # github seam pairs with github-real, independently.
    gh = HarnessSettings(real="storage-github", storage="s3-real")  # type: ignore[call-arg]
    gh_problems = gh.storage_axis_mismatches()
    assert len(gh_problems) == 1
    assert "github-real" in gh_problems[0]


def test_storage_pairing_fails_at_the_collection_gate() -> None:
    # The dual-knob mismatch surfaces through the same loud-fail as missing creds.
    env = {
        "STORAGE_S3_BUCKET": "b",
        "STORAGE_S3_REGION": "r",
        "STORAGE_S3_ACCESS_KEY": "a",
        "STORAGE_S3_SECRET_KEY": "s",
        "STORAGE_S3_ENDPOINT": "https://s3.example",
    }
    with pytest.raises(RealSelectionError, match="TAI_E2E_STORAGE=s3-real"):
        assert_real_selection_ready(_settings("storage-s3"), env)
    # Creds present AND axis paired -> the gate passes.
    assert_real_selection_ready(
        HarnessSettings(real="storage-s3", storage="s3-real"),  # type: ignore[call-arg]
        env,
    )


def test_loud_fail_at_collection_in_subprocess(tmp_path: Path) -> None:
    """End-to-end proof: an actual pytest collection with ``TAI_E2E_REAL=stripe``
    and no creds aborts (nonzero) at ``pytest_configure`` naming the exact vars.

    Runs in an isolated tmp dir so only the published plugin (entry point) is
    active; a trivial test would pass, so a nonzero exit proves the gate fired."""
    (tmp_path / "test_placeholder.py").write_text(
        textwrap.dedent(
            """
            def test_noop():
                assert True
            """
        )
    )
    # A clean env: strip anything that would satisfy the stripe creds or the
    # public-base-URL requirement, then select stripe real.
    env = dict(os.environ)
    for var in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "E2E_PUBLIC_BASE_URL"):
        env.pop(var, None)
    env["TAI_E2E_REAL"] = "stripe"

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--co", "-q", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "STRIPE_SECRET_KEY" in combined, combined
    assert "STRIPE_WEBHOOK_SECRET" in combined, combined
