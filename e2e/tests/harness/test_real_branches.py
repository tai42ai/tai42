"""PLAN_2 — the per-service REAL legs are SELECTED at manifest/variant build time.

The engine's loud-fail-at-collection is proven in ``test_real_switch.py``; these
tests prove the other half: with a seam named on ``TAI_E2E_REAL`` (and its operator
env present), the manifest builder / storage variant renders the REAL branch — the
mock stub coordinates gone, the live-vendor knobs in — and with the seam UNnamed the
render is byte-for-byte the mock default. No live creds and no booted stack: the real
leg is READY, not run (real runs only on the e2e host with the filled creds file).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tai42_e2e.manifests import (
    _STRIPE_TEST_SECRET_KEY,
    ATLASSIAN_CLIENT_ID,
    GOOGLE_CLIENT_ID,
    _llm_env,
    build_payments_stack,
    build_shipped_connectors_stack,
)
from tai42_e2e.marketplace import _marketplace_source_env
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import InfraUnavailable, StackConfig, StackResources, TaiStack, Topology
from tai42_e2e.variants import STORAGES, resolve_variants


def _res(**overrides: object) -> StackResources:
    base: dict[str, object] = {
        "redis_idx": 1,
        "redis_url": "redis://127.0.0.1:6379/1",
        "probe_redis_url": "redis://127.0.0.1:6379/0",
        "pg_host": "127.0.0.1",
        "pg_port": 5432,
        "pg_user": "postgres",
        "pg_password": "postgres",
        "pg_db": "tai42_e2e_probe",
        "storage_root": "/tmp/e2e-storage",
    }
    base.update(overrides)
    return StackResources(**base)  # type: ignore[arg-type]


_VARIANTS = resolve_variants(HarnessSettings(backend="arq", identity="redis", storage="local"))


# ---- llm / embeddings seam (independent toggles) ------------------------


def test_llm_env_mock_default_is_todays_stub_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_E2E_REAL", raising=False)
    env = _llm_env(_res(llm_base_url="http://127.0.0.1:9config/v1"))
    assert env == {
        "LLM_BASE_URL": "http://127.0.0.1:9config/v1",
        "LLM_API_KEY": "e2e-test",
        "LLM_MODEL": "e2e-scripted",
        "EMBEDDING_BASE_URL": "http://127.0.0.1:9config/v1",
        "EMBEDDING_API_KEY": "e2e-test",
        "EMBEDDING_MODEL": "e2e-embed",
    }


def test_llm_real_replaces_only_its_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    # llm real + embeddings mock: LLM_* points at the live provider, EMBEDDING_* still
    # at the stub (independent toggles). Default provider openai.
    monkeypatch.setenv("TAI_E2E_REAL", "llm")
    monkeypatch.delenv("REAL_E2E_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-xyz")
    monkeypatch.delenv("REAL_E2E_LLM_MODEL", raising=False)
    env = _llm_env(_res(llm_base_url="http://127.0.0.1:9/v1"))
    assert env["LLM_PROVIDER_LLM"] == "openai"
    assert env["LLM_API_KEY"] == "sk-live-xyz"
    assert env["LLM_MODEL"] == "gpt-4o-mini"
    assert "LLM_BASE_URL" not in env  # no stub base on the real group
    # embeddings group untouched (still the stub)
    assert env["EMBEDDING_BASE_URL"] == "http://127.0.0.1:9/v1"
    assert env["EMBEDDING_MODEL"] == "e2e-embed"


def test_llm_real_selects_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-default provider: REAL_E2E_LLM_PROVIDER picks anthropic, its own key maps to
    # LLM_API_KEY, LLM_PROVIDER_LLM carries the provider id, and the model falls back to
    # the provider default when REAL_E2E_LLM_MODEL is unset.
    monkeypatch.setenv("TAI_E2E_REAL", "llm")
    monkeypatch.setenv("REAL_E2E_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-live")
    monkeypatch.delenv("REAL_E2E_LLM_MODEL", raising=False)
    env = _llm_env(_res(llm_base_url="http://127.0.0.1:9/v1"))
    assert env["LLM_PROVIDER_LLM"] == "anthropic"
    assert env["LLM_API_KEY"] == "sk-ant-live"
    assert env["LLM_MODEL"] == "claude-haiku-4-5-20251001"


def test_embeddings_real_rejects_chat_only_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # anthropic serves chat but not embeddings — selecting it for the embeddings seam
    # raises loudly (never silently falls back to a default provider).
    monkeypatch.setenv("TAI_E2E_REAL", "embeddings")
    monkeypatch.setenv("REAL_E2E_EMBEDDING_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-live")
    with pytest.raises(ValueError, match="does not serve embeddings"):
        _llm_env(_res(llm_base_url="http://127.0.0.1:9/v1"))


def test_llm_real_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_E2E_REAL", "llm")
    monkeypatch.setenv("REAL_E2E_LLM_PROVIDER", "not-a-provider")
    with pytest.raises(ValueError, match="not a known LLM provider"):
        _llm_env(_res(llm_base_url="http://127.0.0.1:9/v1"))


# ---- stripe seam (env-swap manifest builder) ----------------------------


def test_payments_mock_default_points_at_the_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_E2E_REAL", raising=False)
    cfg = build_payments_stack(_res(stripe_stub_base="http://127.0.0.1:9/stripe"), _VARIANTS)
    assert cfg.env["STRIPE_API_BASE"] == "http://127.0.0.1:9/stripe"
    assert cfg.env["STRIPE_SECRET_KEY"] == _STRIPE_TEST_SECRET_KEY
    assert cfg.public_base_url_env_keys == []
    assert cfg.public_base_url is None


def test_payments_real_drops_the_stub_and_routes_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_E2E_REAL", "stripe")
    monkeypatch.setenv("E2E_PUBLIC_BASE_URL", "https://e2e.example")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_live0000")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_live0000")
    cfg = build_payments_stack(_res(stripe_stub_base="http://127.0.0.1:9/stripe"), _VARIANTS)
    # the stub override is gone (plugin default = real api.stripe.com)
    assert "STRIPE_API_BASE" not in cfg.env
    assert cfg.env["STRIPE_SECRET_KEY"] == "sk_test_live0000"
    assert cfg.env["E2E_STRIPE_WEBHOOK_SECRET"] == "whsec_live0000"
    # the callback the real webhook drives is minted on the public origin
    assert cfg.public_base_url_env_keys == ["INTERACTIONS_PUBLIC_BASE_URL"]
    assert cfg.public_base_url == "https://e2e.example"


# ---- storage-s3 / storage-github real siblings --------------------------


def test_s3_real_variant_feature_env_reads_operator_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_S3_ENDPOINT", "https://ns.compat.objectstorage.example")
    monkeypatch.setenv("STORAGE_S3_BUCKET", "tai-e2e-real")
    monkeypatch.setenv("STORAGE_S3_REGION", "eu-frankfurt-1")
    monkeypatch.setenv("STORAGE_S3_ACCESS_KEY", "AKID")
    monkeypatch.setenv("STORAGE_S3_SECRET_KEY", "SECRET")
    monkeypatch.delenv("STORAGE_S3_ADDRESSING_STYLE", raising=False)
    env = STORAGES["s3-real"].feature_env(_res())
    assert env["STORAGE_S3_ENDPOINT"] == "https://ns.compat.objectstorage.example"
    assert env["STORAGE_S3_BUCKET"] == "tai-e2e-real"
    assert env["STORAGE_S3_ADDRESSING_STYLE"] == "path"  # non-AWS default
    assert env["STORAGE_S3_REQUEST_CHECKSUM_CALCULATION"] == "when_required"


def test_s3_real_variant_loud_fails_naming_missing_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("STORAGE_S3_ENDPOINT", "STORAGE_S3_BUCKET", "STORAGE_S3_REGION", "STORAGE_S3_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STORAGE_S3_SECRET_KEY", "SECRET")  # one present, the rest absent
    with pytest.raises(InfraUnavailable) as exc:
        STORAGES["s3-real"].feature_env(_res())
    msg = str(exc.value)
    assert "STORAGE_S3_ENDPOINT" in msg
    assert "STORAGE_S3_BUCKET" in msg
    assert "storage-s3" in msg


def test_github_real_variant_sets_only_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_GITHUB_USERNAME", "tai42-machine")
    monkeypatch.setenv("STORAGE_GITHUB_REPO", "e2e-store")
    monkeypatch.setenv("STORAGE_GITHUB_TOKEN", "github_pat_xxx")
    monkeypatch.delenv("STORAGE_GITHUB_BRANCH", raising=False)
    env = STORAGES["github-real"].feature_env(_res())
    assert env["STORAGE_GITHUB_USERNAME"] == "tai42-machine"
    assert env["STORAGE_GITHUB_REPO"] == "e2e-store"
    assert env["STORAGE_GITHUB_TOKEN"] == "github_pat_xxx"
    # no RAW/CONTENTS/TREES overrides — the plugin defaults address real GitHub
    assert not any(key.endswith(("RAW_BASE_URL", "CONTENTS_API_URL", "TREES_API_URL")) for key in env)


# ---- marketplace runner ingest sources (independent toggles) ------------

_MP_INDEX = "http://127.0.0.1:9/pkgindex"


def test_marketplace_source_env_mock_default_is_todays_fixture_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_E2E_REAL", raising=False)
    env = _marketplace_source_env(HarnessSettings(), _MP_INDEX)
    assert env == {
        "MP_PYPI_BASE_URL": _MP_INDEX,
        "MP_GITHUB_API_BASE": f"{_MP_INDEX}/gh-api",
    }


def test_marketplace_pypi_real_drops_the_fixture_index(monkeypatch: pytest.MonkeyPatch) -> None:
    # pypi real + github mock: the PyPI override is gone (→ real pypi.org), the github
    # fixture base stays (independent toggles); no token on the pypi-only real leg.
    monkeypatch.setenv("TAI_E2E_REAL", "marketplace-pypi")
    env = _marketplace_source_env(HarnessSettings(), _MP_INDEX)
    assert "MP_PYPI_BASE_URL" not in env
    assert env["MP_GITHUB_API_BASE"] == f"{_MP_INDEX}/gh-api"
    assert "MP_GITHUB_TOKEN" not in env


def test_marketplace_github_real_drops_base_and_carries_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # github real + pypi mock: the github fixture base is gone (→ real api.github.com) and
    # the operator token is carried; the PyPI fixture index stays.
    monkeypatch.setenv("TAI_E2E_REAL", "marketplace-github")
    monkeypatch.setenv("MP_GITHUB_TOKEN", "ghp_live0000")
    env = _marketplace_source_env(HarnessSettings(), _MP_INDEX)
    assert "MP_GITHUB_API_BASE" not in env
    assert env["MP_GITHUB_TOKEN"] == "ghp_live0000"
    assert env["MP_PYPI_BASE_URL"] == _MP_INDEX


# ---- connector redirect-allowlist public-origin routing -----------------


def test_connectors_mock_default_keeps_loopback_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_E2E_REAL", raising=False)
    cfg = build_shipped_connectors_stack(_res(), _VARIANTS)
    assert cfg.env["CONNECTORS_GOOGLE_CLIENT_ID"] == GOOGLE_CLIENT_ID
    assert cfg.env["CONNECTORS_ATLASSIAN_CLIENT_ID"] == ATLASSIAN_CLIENT_ID
    # the allowlist key stays boot-filled from the loopback origin (no public override)
    assert cfg.origin_allowlist_env_keys == ["CONNECTORS_REDIRECT_URI_ALLOWLIST"]
    assert cfg.public_allowlist_env_keys == []
    assert cfg.public_base_url is None


def test_connectors_real_routes_allowlist_to_public_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_E2E_REAL", "connector-google")
    monkeypatch.setenv("E2E_PUBLIC_BASE_URL", "https://e2e.example")
    monkeypatch.setenv("CONNECTORS_GOOGLE_CLIENT_ID", "live-google-id")
    monkeypatch.setenv("CONNECTORS_GOOGLE_CLIENT_SECRET", "live-google-secret")
    cfg = build_shipped_connectors_stack(_res(), _VARIANTS)
    assert cfg.env["CONNECTORS_GOOGLE_CLIENT_ID"] == "live-google-id"
    # atlassian still mock (independent toggle)
    assert cfg.env["CONNECTORS_ATLASSIAN_CLIENT_ID"] == ATLASSIAN_CLIENT_ID
    # the same allowlist key now routes to the PUBLIC origin the OAuth redirect is registered at
    assert cfg.public_allowlist_env_keys == ["CONNECTORS_REDIRECT_URI_ALLOWLIST"]
    assert cfg.public_base_url == "https://e2e.example"


def _allowlist_cfg(*, public: bool) -> StackConfig:
    return StackConfig(
        name="c",
        topology=Topology.MULTIWORKER,
        manifest={},
        env={},
        origin_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
        public_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"] if public else [],
        public_base_url="https://e2e.example/" if public else None,
    )


def test_origin_allowlist_engine_hook_public_vs_loopback() -> None:
    # The stack-level fill: a public-listed key carries the public origin; an unlisted key
    # (the mock default) carries this stack's loopback app origins. Mirrors _replica_b_origin_env.
    public_stub = SimpleNamespace(config=_allowlist_cfg(public=True), host="127.0.0.1", app_ports=[8001, 8002])
    assert TaiStack._origin_allowlist_env(public_stub) == {  # type: ignore[arg-type]
        "CONNECTORS_REDIRECT_URI_ALLOWLIST": "https://e2e.example"
    }
    loopback_stub = SimpleNamespace(config=_allowlist_cfg(public=False), host="127.0.0.1", app_ports=[8001, 8002])
    assert TaiStack._origin_allowlist_env(loopback_stub) == {  # type: ignore[arg-type]
        "CONNECTORS_REDIRECT_URI_ALLOWLIST": "http://127.0.0.1:8001,http://127.0.0.1:8002"
    }


def test_origin_allowlist_public_without_base_url_raises() -> None:
    stub = SimpleNamespace(
        config=StackConfig(
            name="c",
            topology=Topology.MULTIWORKER,
            manifest={},
            env={},
            origin_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
            public_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
            public_base_url=None,
        ),
        host="127.0.0.1",
        app_ports=[8001],
    )
    with pytest.raises(RuntimeError, match="public_base_url is unset"):
        TaiStack._origin_allowlist_env(stub)  # type: ignore[arg-type]
