"""B2 support — the config-k8s stack builder and its fixed K8s coordinates.

A bare-name sibling module (the suite's convention). The profile boots a real
``tai serve`` fleet in ``TAI_CONFIG_MODE=k8s``: the manifest is read from the fake
apiserver's ConfigMap (``read_manifest`` at boot) rather than the seeded file, and
the config router's env/manifest write doors drive the Secret/ConfigMap PATCH paths.

k8s config mode is refused without the worker bus (a pod cannot see its own replica
count), so the profile is MULTIWORKER with two workers — which wires the bus — and
runs no backend."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from tai42_e2e.manifests import _base_env, _toolbox_tools_entry
from tai42_e2e.stack import StackConfig, StackResources, Topology
from tai42_e2e.variants import Variants

# The K8s coordinates the SUT and the fake apiserver share. Names are the provider's
# own defaults (``settings.py``); only the namespace is pinned so the fake serves it.
K8S_NAMESPACE = "e2e"
K8S_SECRET_NAME = "tai-env"
K8S_CONFIGMAP_NAME = "tai-manifest"
K8S_MANIFEST_KEY = "manifest.yml"


def build_config_k8s_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(2), no backend — a minimal router surface (health/metrics/tools plus
    the config + manifest doors) booted under ``TAI_CONFIG_MODE=k8s``.

    The config router mounts ``/api/config/{mode,env}`` and the manifest router mounts
    ``/api/manifest`` + ``/api/manifest/replace`` — the doors the reload assertions drive
    through the K8s Secret and ConfigMap. ``KUBECONFIG`` is filled in by the fixture once
    the fake apiserver's port is known; every other K8s coordinate is set here."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [
            "tai42_skeleton.routers.health",
            "tai42_skeleton.routers.metrics",
            "tai42_skeleton.routers.tools",
            "tai42_skeleton.routers.config",
            "tai42_skeleton.routers.manifest",
        ],
        # A single real tool so the tools door has something to answer for; api_tools off
        # keeps the surface minimal — the config-mode boot itself is what is under test.
        "tools": [_toolbox_tools_entry()],
        "api_tools": {"enabled": False},
    }
    env = _base_env(res, variants)
    env["TAI_CONFIG_MODE"] = "k8s"
    env["TAI_K8S_NAMESPACE"] = K8S_NAMESPACE
    env["TAI_K8S_SECRET_NAME"] = K8S_SECRET_NAME
    env["TAI_K8S_CONFIGMAP_NAME"] = K8S_CONFIGMAP_NAME
    env["TAI_K8S_MANIFEST_KEY"] = K8S_MANIFEST_KEY
    return StackConfig(
        name="config-k8s",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=2,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def real_k8s_config(config: StackConfig, environ: Mapping[str, str]) -> StackConfig:
    """REAL k8s: point the stack's ``KUBECONFIG`` + ``TAI_K8S_NAMESPACE`` at the
    operator's live cluster.

    The fixture calls this instead of standing up the fake apiserver: the fake is NOT
    started and nothing is seeded (the ConfigMap + Secret are a vendor-side prerequisite),
    so the SUT reads its manifest and drives the reload doors against the real cluster.
    The namespace comes from the operator's ``TAI_K8S_NAMESPACE`` (the fake-apiserver leg's
    pinned ``e2e`` is meaningless against a real cluster — the operator seeds the objects in
    the namespace the kubeconfig identity has get+patch on); the object names the builder set
    stand unchanged. Both vars are required at collection (``REAL_SERVICES["k8s"]``)."""
    return dataclasses.replace(
        config,
        env={**config.env, "KUBECONFIG": environ["KUBECONFIG"], "TAI_K8S_NAMESPACE": environ["TAI_K8S_NAMESPACE"]},
    )
