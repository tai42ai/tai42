"""Recycle capability report + two-tier refusal policy.

The ONE source of truth the profile-apply validator and the tai-distribution parity
tests import: shape detection from the ``TAI_SUPERVISED`` marker, the tier-1
bus-URL refusal on every shape, the per-shape tier-2 pinned lists, and the
X-classification of the two deployment-infra bare reads.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.config.recycle_policy import (
    CENSUS_TARGET_KINDS,
    TIER1_REFUSED_KEYS,
    TIER2_COMPOSE_REFUSED_KEYS,
    TIER2_K8S_REFUSED_KEYS,
    X_CLASSIFIED_DEPLOYMENT_BARE_READS,
    Shape,
    capability_report,
    detect_shape,
    refused_keys,
)


@pytest.fixture(autouse=True)
def _clear_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)


# -- shape detection ----------------------------------------------------------


def test_absent_marker_is_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    assert detect_shape() is Shape.bare


@pytest.mark.parametrize("marker", ["k8s", "compose", "harness"])
def test_marker_maps_to_its_shape(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", marker)
    assert detect_shape() is Shape(marker)


def test_whitespace_marker_is_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "  ")
    assert detect_shape() is Shape.bare


@pytest.mark.parametrize("marker", ["bare", "docker", "K8S", "kubernetes"])
def test_unrecognized_marker_raises_loudly(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    # An explicit "bare" is NOT a marker, and a typo must never silently degrade to
    # bare (which would skip recycle refusal) — every non-empty non-marker raises.
    monkeypatch.setenv("TAI_SUPERVISED", marker)
    with pytest.raises(ValueError, match="TAI_SUPERVISED"):
        detect_shape()


# -- capability report per shape ----------------------------------------------


def test_bare_is_unsupported_tier1_only(monkeypatch: pytest.MonkeyPatch) -> None:
    report = capability_report()
    assert report.shape is Shape.bare
    assert report.recycle_supported is False
    assert set(report.refused_keys) == set(TIER1_REFUSED_KEYS)
    assert report.census_kinds == list(CENSUS_TARGET_KINDS)


def test_harness_supported_tier1_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    report = capability_report()
    assert report.shape is Shape.harness
    assert report.recycle_supported is True
    # Tier 1 rides EVERY shape including harness; harness carries NO tier-2 list.
    assert set(report.refused_keys) == set(TIER1_REFUSED_KEYS)


def test_k8s_refuses_tier1_plus_the_k8s_pinned_union(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "k8s")
    report = capability_report()
    assert report.shape is Shape.k8s
    assert report.recycle_supported is True
    refused = set(report.refused_keys)
    assert refused >= TIER1_REFUSED_KEYS
    assert refused >= TIER2_K8S_REFUSED_KEYS
    # Representative pinned helper keys from the union across BOTH deployments.
    assert {"SUB_MCP_REDIS_URL", "ARQ_REDIS_URL", "CELERY_BROKER_URL"} <= refused


def test_compose_refuses_tier1_plus_the_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "compose")
    report = capability_report()
    assert report.shape is Shape.compose
    refused = set(report.refused_keys)
    assert refused >= TIER1_REFUSED_KEYS
    assert refused >= TIER2_COMPOSE_REFUSED_KEYS
    assert {"TAI_BACKEND_MODULE", "STORAGE_S3_BUCKET"} <= refused


# -- two-tier structure -------------------------------------------------------


def test_tier1_is_the_two_bus_reaching_urls() -> None:
    assert frozenset({"TAI_BUS_REDIS_URL", "TAI_DEFAULT_REDIS_URL"}) == TIER1_REFUSED_KEYS


@pytest.mark.parametrize("shape", list(Shape))
def test_tier1_rides_every_shape(shape: Shape) -> None:
    assert refused_keys(shape) >= TIER1_REFUSED_KEYS


def test_harness_and_bare_carry_no_tier2() -> None:
    assert refused_keys(Shape.harness) == TIER1_REFUSED_KEYS
    assert refused_keys(Shape.bare) == TIER1_REFUSED_KEYS


def test_k8s_tier2_excludes_the_bus_url_and_x_band_keys() -> None:
    # Tier-1 bus URL is not duplicated into the tier-2 list, and the X-band keys the
    # helpers also carry are never recyclable (carried untouched), so they are absent.
    assert "TAI_BUS_REDIS_URL" not in TIER2_K8S_REFUSED_KEYS
    for x_key in ("TAI_CONFIG_MODE", "TAI_PLUGINS_PREFIX", "PROMETHEUS_MULTIPROC_DIR"):
        assert x_key not in TIER2_K8S_REFUSED_KEYS


def test_compose_tier2_enumerates_the_anchor_verbatim() -> None:
    # The anchor carries these even though they are X-band / tier-1 on other axes; the
    # compose refusal enumerates the anchor as-is (parity with the compose file).
    assert {"TAI_CONFIG_MODE", "TAI_BUS_REDIS_URL", "PROMETHEUS_MULTIPROC_DIR"} <= TIER2_COMPOSE_REFUSED_KEYS


def test_both_sandbox_provider_keys_are_tier2_recycle_class_on_both_shapes() -> None:
    # Both sandbox providers' connection keys are recycle-class regardless of which is
    # active (mutually exclusive at runtime via the scalar module), so both coexist in
    # both tier-2 lists — mirroring the ARQ_REDIS_URL precedent.
    for key in ("SANDBOX_DOCKER_HOST", "SANDBOX_LOCAL_ROOT"):
        assert key in TIER2_K8S_REFUSED_KEYS
        assert key in TIER2_COMPOSE_REFUSED_KEYS


def test_sandbox_selecting_env_is_not_recycle_class() -> None:
    # The provider loads via the manifest ``sandbox_module``, not this env — so it is
    # deliberately absent from both lists, mirroring ``TAI_MCP_BACKEND`` / ``TAI_BACKEND_MODULE``.
    assert "TAI_MCP_SANDBOX" not in TIER2_K8S_REFUSED_KEYS
    assert "TAI_MCP_SANDBOX" not in TIER2_COMPOSE_REFUSED_KEYS


# -- census kinds -------------------------------------------------------------


def test_census_target_kinds_are_backend_then_serve() -> None:
    assert (WorkerKind.backend, WorkerKind.serve) == CENSUS_TARGET_KINDS


# -- X-classification of the deployment-infra bare reads ----------------------


def test_deployment_bare_reads_are_x_classified() -> None:
    # A profile can NEVER carry these; the boundary validator folds this set into its
    # X-band refusal enforced at every env writer.
    assert frozenset({"TAI_SUPERVISED", "TAI_READY_SENTINEL_PATH"}) == X_CLASSIFIED_DEPLOYMENT_BARE_READS
