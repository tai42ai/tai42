"""B2 — the config-k8s stack fixture: a real ``tai serve`` fleet in k8s config mode.

Wires the threaded fake apiserver (``_fake_k8s.FakeKubernetes``) to the SUT: seeds the
ConfigMap with the profile's manifest (byte-for-byte the dump the file path would write,
so file-mode and k8s-mode agree) and an empty Secret, writes an HTTP kubeconfig, points
the stack's ``KUBECONFIG`` at it, then boots. The fake is fully self-contained here — no
top-level conftest wiring is needed; only the SUT venv must carry ``tai42-config-k8s`` and
its ``kubernetes`` client for the boot to reach the provider."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator

import pytest
import yaml

from tai42_e2e import diagnostics
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.harness import release_resources
from tai42_e2e.stack import Infra, TaiStack

from ._config_k8s_support import (  # pyright: ignore[reportMissingImports]
    K8S_CONFIGMAP_NAME,
    K8S_MANIFEST_KEY,
    K8S_NAMESPACE,
    K8S_SECRET_NAME,
    build_config_k8s_stack,
    real_k8s_config,
)
from ._fake_k8s import FakeKubernetes  # pyright: ignore[reportMissingImports]

# No backend worker: this profile is route-mounting + config-mode only.
pytestmark = pytest.mark.backendless


@pytest.fixture(scope="module")
def config_k8s_stack(
    infra: Infra, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TaiStack, FakeKubernetes | None]]:
    root = tmp_path_factory.mktemp("config-k8s")
    resources, config = allocate_and_build(infra, root, build_config_k8s_stack, None, False)
    if infra.settings.is_real("k8s"):
        # REAL: the SUT reads its manifest from the operator's live cluster and drives the
        # reload doors against its real Secret/ConfigMap, so the fake apiserver is NOT started
        # and nothing is seeded here (the ConfigMap + Secret are a vendor-side prerequisite).
        # KUBECONFIG points at the real cluster from the operator env; the namespace + object
        # names carried by the builder are the SUT/cluster contract. The fake handle is absent
        # (None) — the fake-introspection tests are the mock leg and step aside for a real one.
        config = real_k8s_config(config, os.environ)
        try:
            stack = TaiStack(config, infra, resources, root)
        except BaseException:
            release_resources(infra, resources)
            raise
        with stack, diagnostics.track(stack):
            yield stack, None
        return
    # MOCK (default) — the threaded fake apiserver path, byte-for-byte as today.
    fake = FakeKubernetes(namespace=K8S_NAMESPACE, secret_name=K8S_SECRET_NAME, configmap_name=K8S_CONFIGMAP_NAME)
    try:
        fake.start()
        # Seed the ConfigMap with the manifest the SUT reads at boot, and an empty Secret
        # the reload path reads/patches. Point KUBECONFIG at the generated http kubeconfig.
        fake.seed_manifest(K8S_MANIFEST_KEY, yaml.safe_dump(config.manifest, sort_keys=False))
        fake.seed_secret({})
        kubeconfig = root / "kubeconfig.yaml"
        fake.write_kubeconfig(kubeconfig)
        config = dataclasses.replace(config, env={**config.env, "KUBECONFIG": str(kubeconfig)})
        stack = TaiStack(config, infra, resources, root)
    except BaseException:
        # Nothing owns the allocation until TaiStack exists — reap it, then the fake.
        release_resources(infra, resources)
        fake.stop()
        raise
    try:
        with stack, diagnostics.track(stack):
            yield stack, fake
    finally:
        fake.stop()
