"""B2 — config load + reload through the config-k8s provider against a fake apiserver.

Replaces the boot-refusal-only coverage (``tests/fleet/test_boot_rules.py``, which
only asserts a busless k8s boot is refused) with the positive path: a stack that BOOTS
in ``TAI_CONFIG_MODE=k8s``, reads its manifest from the ConfigMap through the plugin, and
whose env/manifest writes round-trip through the Secret/ConfigMap PATCH surface —
including the ConfigMap ``resourceVersion`` optimistic-concurrency precondition."""

from __future__ import annotations

import pytest
from _config_k8s_support import K8S_MANIFEST_KEY  # pyright: ignore[reportMissingImports]
from _fake_k8s import FakeKubernetes  # pyright: ignore[reportMissingImports]

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# These assertions read the FAKE apiserver's introspection counters (ConfigMap reads,
# Secret/ConfigMap PATCHes, the armed-conflict retry), so they are the k8s MOCK leg. A real
# k8s selection points the stack at a live cluster with no such counters and reads back
# differently, so the fake-bound suite steps aside for it. Inert (no-op) in the default
# mock run — is_real("k8s") is False, so collection is byte-for-byte today's.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("k8s"),
    reason="fake-apiserver ConfigMap/Secret round-trip is the k8s mock leg; a real cluster leg reads back differently",
)


async def test_config_k8s_load_and_reload(config_k8s_stack: tuple[TaiStack, FakeKubernetes | None]) -> None:
    stack, fake = config_k8s_stack
    assert fake is not None  # the mock leg always carries the fake (real selection is skipped above)
    api = stack.api()

    # LOAD: the fleet booted in k8s config mode, reading the manifest from the ConfigMap
    # through the plugin (not from the seeded file).
    mode = await api.get("/api/config/mode")
    assert "k8s" in str(mode), mode
    assert fake.configmap_reads >= 1, "boot did not read the manifest from the ConfigMap through the plugin"
    live_manifest = await api.get("/api/manifest")
    assert live_manifest, f"the live manifest read back empty: {live_manifest!r}"

    # RELOAD (env): a config-env change PATCHes the Secret (write_env), the reload re-reads
    # it, and the value round-trips back out of the Secret.
    await api.post("/api/config/env", json={"E2E_K8S_PROBE": "reload-v1"})
    assert fake.secret_patches >= 1, "the env change did not PATCH the Secret"
    assert fake.secret_env().get("E2E_K8S_PROBE") == "reload-v1", fake.secret_env()
    env_view = await api.get("/api/config/env")
    assert "E2E_K8S_PROBE" in str(env_view), env_view

    # RELOAD (manifest): replacing the manifest PATCHes the ConfigMap under the
    # resourceVersion precondition — the PATCH carries the current resourceVersion and the
    # store bumps it (a stale one would 409, driving the manager's retry loop).
    before_rv = fake.configmap_resource_version()
    current_text = fake.configmap_manifest_text(K8S_MANIFEST_KEY)
    await api.post("/api/manifest/replace", json={"manifest_text": current_text})
    assert fake.configmap_patches >= 1, "the manifest replace did not PATCH the ConfigMap"
    assert fake.last_configmap_patch_rv == before_rv, (
        f"the ConfigMap PATCH did not carry the read resourceVersion: "
        f"sent {fake.last_configmap_patch_rv!r}, expected {before_rv!r}"
    )
    assert fake.configmap_resource_version() != before_rv, "the ConfigMap resourceVersion was not bumped by the PATCH"


async def test_manifest_replace_retries_on_stale_resource_version(
    config_k8s_stack: tuple[TaiStack, FakeKubernetes | None],
) -> None:
    """The 409 → re-read → retry loop lands the write despite a stale resourceVersion.

    A one-shot competing write bumps the ConfigMap rv between the manager's read and its
    PATCH, so the first PATCH carries a now-stale rv and 409s. The manager must re-read the
    fresh rv and re-apply — last writer converges — rather than surfacing the conflict."""
    stack, fake = config_k8s_stack
    assert fake is not None  # the mock leg always carries the fake (real selection is skipped above)
    api = stack.api()

    before_reads = fake.configmap_reads
    before_conflicts = fake.configmap_conflicts
    before_patches = fake.configmap_patches
    current_text = fake.configmap_manifest_text(K8S_MANIFEST_KEY)

    fake.arm_stale_conflict()
    await api.post("/api/manifest/replace", json={"manifest_text": current_text})

    # The stale rv was rejected once (the retry loop was genuinely driven, not a clean write).
    assert fake.configmap_conflicts == before_conflicts + 1, (
        f"the stale rv did not trigger exactly one 409: {fake.configmap_conflicts - before_conflicts}"
    )
    # The manager re-read the ConfigMap after the 409 (at least two reads: the stale one + the retry).
    assert fake.configmap_reads >= before_reads + 2, "the manager did not re-read the ConfigMap after the 409"
    # And the retried PATCH landed — the replace converged despite the conflict.
    assert fake.configmap_patches == before_patches + 1, "the retried manifest replace did not land"
    live_manifest = await api.get("/api/manifest")
    assert live_manifest, f"the manifest read back empty after the retried replace: {live_manifest!r}"
