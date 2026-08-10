"""B2 — config load + reload through the config-k8s provider against a fake apiserver.

Replaces the boot-refusal-only coverage (``tests/fleet/test_boot_rules.py``, which
only asserts a busless k8s boot is refused) with the positive path: a stack that BOOTS
in ``TAI_CONFIG_MODE=k8s``, reads its manifest from the ConfigMap through the plugin, and
whose env/manifest writes round-trip through the Secret/ConfigMap PATCH surface —
including the ConfigMap ``resourceVersion`` optimistic-concurrency precondition."""

from __future__ import annotations

from collections.abc import Callable

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

    # RELOAD (env): a config-env change REPLACES the Secret (write_env is a whole-object
    # ``replace_namespaced_secret`` PUT that merges existing+change in ``build_desired``),
    # the reload re-reads it, and the value round-trips back out of the Secret.
    await api.post("/api/config/env", json={"E2E_K8S_PROBE": "reload-v1"})
    assert fake.secret_replaces >= 1, "the env change did not write the Secret (replace_namespaced_secret PUT)"
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


async def test_env_write_retries_on_stale_secret_resource_version(
    config_k8s_stack: tuple[TaiStack, FakeKubernetes | None],
) -> None:
    """The Secret write's 409 → re-read → retry loop lands despite a stale resourceVersion.

    An env write is a whole-object ``replace_namespaced_secret`` (PUT) under the Secret's own
    rv precondition; a one-shot competing write bumps the rv between the manager's read and
    its PUT, so the first PUT 409s and ``_commit_secret_with_retry`` must re-read + re-apply
    on the fresh rv rather than surfacing the conflict."""
    stack, fake = config_k8s_stack
    assert fake is not None  # the mock leg always carries the fake (real selection is skipped above)
    api = stack.api()

    before_reads = fake.secret_reads
    before_conflicts = fake.secret_conflicts
    before_replaces = fake.secret_replaces

    fake.arm_secret_stale_conflict()
    await api.post("/api/config/env", json={"E2E_K8S_RETRY": "landed"})

    # The stale rv was rejected exactly once (the retry loop was genuinely driven).
    assert fake.secret_conflicts == before_conflicts + 1, (
        f"the stale rv did not trigger exactly one Secret 409: {fake.secret_conflicts - before_conflicts}"
    )
    # The manager re-read the Secret after the 409 (at least two reads: the stale one + retry).
    assert fake.secret_reads >= before_reads + 2, "the manager did not re-read the Secret after the 409"
    # And the retried PUT landed — the write converged despite the conflict.
    assert fake.secret_replaces == before_replaces + 1, "the retried Secret write did not land"
    assert fake.secret_env().get("E2E_K8S_RETRY") == "landed", fake.secret_env()


async def test_profile_apply_replaces_secret_whole_object(
    config_k8s_stack: tuple[TaiStack, FakeKubernetes | None], uniq: Callable[[str], str]
) -> None:
    """A settings-profile apply drives ``replace_env`` — a whole-object Secret PUT whose
    ``stringData`` becomes the ENTIRE Secret data. A key present before the apply but ABSENT
    from the profile is DELETED (the resulting-state gap a merge-PATCH could never close);
    ``resourceVersion`` still guards the write. The profile store is the stack's own PG
    component store, independent of the k8s config backend the Secret PUT rides."""
    stack, fake = config_k8s_stack
    assert fake is not None  # the mock leg always carries the fake (real selection is skipped above)
    api = stack.api()

    # Seed two stored keys through a normal env write, then a profile that keeps + changes
    # one, adds one, and OMITS the other.
    await api.post("/api/config/env", json={"E2E_K8S_KEEP": "old", "E2E_K8S_DROP": "gone"})
    assert fake.secret_env().get("E2E_K8S_DROP") == "gone", fake.secret_env()

    name = uniq("k8sprofile")
    await api.put(
        f"/api/config/profiles/{name}",
        json={"env": {"E2E_K8S_KEEP": "new", "E2E_K8S_ADD": "added"}},
        retry_on_reloading=True,
    )
    await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True)

    # Whole-object replace: the omitted key is GONE, the kept key took its new value, the
    # added key is present — the Secret holds exactly the profile's declared env.
    secret_env = fake.secret_env()
    assert secret_env.get("E2E_K8S_KEEP") == "new", secret_env
    assert secret_env.get("E2E_K8S_ADD") == "added", secret_env
    assert "E2E_K8S_DROP" not in secret_env, f"whole-object replace did not delete the omitted key: {secret_env}"
