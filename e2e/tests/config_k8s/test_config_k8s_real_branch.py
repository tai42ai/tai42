"""PLAN_2 — the k8s REAL leg is SELECTED at fixture-build time.

The mock leg (``test_config_k8s_stack``) boots against the threaded fake apiserver. This
proves the other half without a cluster: with ``k8s`` named on ``TAI_E2E_REAL`` the fixture
takes the real branch — ``real_k8s_config`` points the stack's ``KUBECONFIG`` at the
operator's live cluster and the fake apiserver is not started — and with the seam UNnamed
the predicate is False so the fake path stands byte-for-byte. No cluster and no creds: the
real leg is READY, not run.
"""

from __future__ import annotations

from _config_k8s_support import real_k8s_config  # pyright: ignore[reportMissingImports]

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import StackConfig, Topology


def _k8s_cfg() -> StackConfig:
    # The shape the builder emits: config-mode k8s + the shared K8s coordinates, no KUBECONFIG
    # yet (the fixture fills it — the fake's http kubeconfig for mock, the operator env for real).
    return StackConfig(
        name="config-k8s",
        topology=Topology.MULTIWORKER,
        manifest={},
        env={"TAI_CONFIG_MODE": "k8s", "TAI_K8S_NAMESPACE": "e2e"},
    )


def test_k8s_selection_predicate() -> None:
    assert HarnessSettings(real="k8s").is_real("k8s") is True  # type: ignore[call-arg]
    assert HarnessSettings(real="").is_real("k8s") is False  # type: ignore[call-arg]


def test_real_k8s_config_points_at_operator_kubeconfig_and_namespace() -> None:
    cfg = _k8s_cfg()
    real = real_k8s_config(cfg, {"KUBECONFIG": "/home/op/.kube/real-cluster", "TAI_K8S_NAMESPACE": "tai-e2e"})
    # KUBECONFIG now addresses the live cluster; the config-mode stands.
    assert real.env["KUBECONFIG"] == "/home/op/.kube/real-cluster"
    assert real.env["TAI_CONFIG_MODE"] == "k8s"
    # the namespace is the operator's (where the ConfigMap/Secret were seeded), NOT the
    # fake-apiserver leg's pinned ``e2e``.
    assert real.env["TAI_K8S_NAMESPACE"] == "tai-e2e"
    # the source config is untouched (frozen replace), so nothing leaks into the mock path
    assert "KUBECONFIG" not in cfg.env
    assert cfg.env["TAI_K8S_NAMESPACE"] == "e2e"


def test_k8s_real_leg_requires_namespace_at_collection() -> None:
    # The loud-fail registry names TAI_K8S_NAMESPACE alongside KUBECONFIG, so a real k8s
    # selection missing the operator namespace aborts at collection rather than silently
    # patching the wrong (fake-leg ``e2e``) namespace against the live cluster.
    s = HarnessSettings(real="k8s")  # type: ignore[call-arg]
    assert s.missing_real_creds({"KUBECONFIG": "/x"}) == {"k8s": ["TAI_K8S_NAMESPACE"]}
    assert s.missing_real_creds({"KUBECONFIG": "/x", "TAI_K8S_NAMESPACE": "tai-e2e"}) == {}
