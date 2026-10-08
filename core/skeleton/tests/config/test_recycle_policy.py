"""Recycle capability report + two-tier refusal policy.

Shape detection from the ``TAI_SUPERVISED`` marker, the tier-1 bus-URL refusal on every
shape, the supervised deployment's declared pinned set (``TAI_SUPERVISED_PINNED_KEYS``),
the boot check that a supervised marker comes with its pinned set, and the
X-classification of the deployment-infra bare reads.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.config.recycle_policy import (
    CENSUS_TARGET_KINDS,
    PINNED_KEYS_ENV,
    TIER1_REFUSED_KEYS,
    X_CLASSIFIED_DEPLOYMENT_BARE_READS,
    Shape,
    capability_report,
    detect_shape,
    pinned_keys,
    refused_keys,
    require_supervision_declared,
)


@pytest.fixture(autouse=True)
def _clear_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    monkeypatch.delenv("TAI_SUPERVISED_PINNED_KEYS", raising=False)


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


@pytest.mark.parametrize("marker", ["k8s", "compose"])
def test_supervised_refuses_tier1_plus_the_declared_pinned_set(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", marker)
    monkeypatch.setenv("TAI_SUPERVISED_PINNED_KEYS", '["SUB_MCP_REDIS_URL", "STORAGE_S3_BUCKET"]')
    report = capability_report()
    assert report.shape is Shape(marker)
    assert report.recycle_supported is True
    assert set(report.refused_keys) == TIER1_REFUSED_KEYS | {"SUB_MCP_REDIS_URL", "STORAGE_S3_BUCKET"}


def test_an_empty_pinned_set_is_legal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "compose")
    monkeypatch.setenv("TAI_SUPERVISED_PINNED_KEYS", "[]")
    require_supervision_declared()
    assert refused_keys(Shape.compose) == TIER1_REFUSED_KEYS


# -- the declared pinned set ----------------------------------------------------


def test_pinned_keys_env_name() -> None:
    assert PINNED_KEYS_ENV == "TAI_SUPERVISED_PINNED_KEYS"


def test_pinned_keys_reads_the_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED_PINNED_KEYS", '["A", "B", "A"]')
    assert pinned_keys() == frozenset({"A", "B"})


@pytest.mark.parametrize("raw", ["A,B", '{"A": 1}', '"A"', '["A", 3]', '["A", ""]', "[null]", ""])
def test_malformed_pinned_keys_raise(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("TAI_SUPERVISED_PINNED_KEYS", raw)
    with pytest.raises(ValueError, match=r"TAI_SUPERVISED_PINNED_KEYS must be a JSON list of env names, got"):
        pinned_keys()


def test_missing_pinned_keys_raise_when_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "compose")
    with pytest.raises(RuntimeError, match=r"TAI_SUPERVISED=compose requires TAI_SUPERVISED_PINNED_KEYS"):
        refused_keys(Shape.compose)


@pytest.mark.parametrize("shape", list(Shape))
def test_tier1_rides_every_shape(monkeypatch: pytest.MonkeyPatch, shape: Shape) -> None:
    monkeypatch.setenv("TAI_SUPERVISED_PINNED_KEYS", "[]")
    assert refused_keys(shape) >= TIER1_REFUSED_KEYS


def test_harness_and_bare_carry_tier1_only() -> None:
    assert refused_keys(Shape.harness) == TIER1_REFUSED_KEYS
    assert refused_keys(Shape.bare) == TIER1_REFUSED_KEYS


def test_tier1_is_the_two_bus_reaching_urls() -> None:
    assert frozenset({"TAI_BUS_REDIS_URL", "TAI_DEFAULT_REDIS_URL"}) == TIER1_REFUSED_KEYS


# -- the boot check -------------------------------------------------------------


@pytest.mark.parametrize("marker", ["k8s", "compose"])
def test_boot_refuses_a_supervised_marker_without_its_pinned_set(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", marker)
    with pytest.raises(RuntimeError) as exc:
        require_supervision_declared()
    assert str(exc.value) == (
        f"TAI_SUPERVISED={marker} requires TAI_SUPERVISED_PINNED_KEYS "
        "(a JSON list of the env keys this deployment pins)"
    )


def test_boot_refuses_a_malformed_pinned_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "compose")
    monkeypatch.setenv("TAI_SUPERVISED_PINNED_KEYS", "A,B")
    with pytest.raises(ValueError, match="TAI_SUPERVISED_PINNED_KEYS must be a JSON list"):
        require_supervision_declared()


@pytest.mark.parametrize("marker", ["harness", None])
def test_boot_refuses_a_pinned_set_without_a_supervised_marker(
    monkeypatch: pytest.MonkeyPatch, marker: str | None
) -> None:
    if marker is not None:
        monkeypatch.setenv("TAI_SUPERVISED", marker)
    monkeypatch.setenv("TAI_SUPERVISED_PINNED_KEYS", "[]")
    with pytest.raises(RuntimeError) as exc:
        require_supervision_declared()
    assert str(exc.value) == "TAI_SUPERVISED_PINNED_KEYS is set but TAI_SUPERVISED is not k8s or compose"


def test_boot_refuses_a_bad_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "docker")
    with pytest.raises(ValueError, match="TAI_SUPERVISED"):
        require_supervision_declared()


@pytest.mark.parametrize("marker", ["harness", None])
def test_boot_passes_an_unsupervised_or_harness_shape(monkeypatch: pytest.MonkeyPatch, marker: str | None) -> None:
    if marker is not None:
        monkeypatch.setenv("TAI_SUPERVISED", marker)
    require_supervision_declared()


def test_boot_passes_a_supervised_marker_with_its_pinned_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "k8s")
    monkeypatch.setenv("TAI_SUPERVISED_PINNED_KEYS", '["SUB_MCP_REDIS_URL"]')
    require_supervision_declared()


# -- census kinds -------------------------------------------------------------


def test_census_target_kinds_are_backend_then_serve() -> None:
    assert (WorkerKind.backend, WorkerKind.serve) == CENSUS_TARGET_KINDS


# -- X-classification of the deployment-infra bare reads ----------------------


def test_deployment_bare_reads_are_x_classified() -> None:
    # A profile can NEVER carry these; the boundary validator folds this set into its
    # X-band refusal enforced at every env writer (a profile must not shrink the pinned set).
    assert (
        frozenset({"TAI_SUPERVISED", "TAI_SUPERVISED_PINNED_KEYS", "TAI_READY_SENTINEL_PATH"})
        == X_CLASSIFIED_DEPLOYMENT_BARE_READS
    )


def test_pinned_keys_unset_raises() -> None:
    with pytest.raises(RuntimeError, match="TAI_SUPERVISED_PINNED_KEYS is not set"):
        pinned_keys()


def test_app_context_refuses_boot_on_a_supervised_marker_without_its_pinned_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from tai42_skeleton.app.instance import app
    from tai42_skeleton.manifest import Manifest

    monkeypatch.setenv("TAI_SUPERVISED", "compose")

    async def boot() -> None:
        async with app.app_context(Manifest.model_validate({})):
            pass

    with pytest.raises(RuntimeError, match="TAI_SUPERVISED=compose requires TAI_SUPERVISED_PINNED_KEYS"):
        asyncio.run(boot())
