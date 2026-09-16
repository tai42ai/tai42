"""Fixtures for the access-control suite.

The partial-restore journey (``test_partial_restore``) needs the first-key bootstrap
door AND the backup export/import doors on one deployment: the bootstrap door is the
platform's sole recovery entrance when only orphaned rows remain, and the backup doors
carry the export/re-mint round trip. The shared ``keys_bootstrap_stack`` mounts a lean
router set without the backup router, so a dedicated stack adds it — only additively over
``build_keys_bootstrap_stack``, mutating no shared builder (the trigger-link suite adds
its own backup router the same way).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

from tai42_e2e import Infra, StackConfig, StackResources
from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import build_keys_bootstrap_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.variants import Variants


def build_keys_bootstrap_backup_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The keys-bootstrap profile PLUS the backup router — the fresh, keyless install whose
    first-key door serves recovery and whose backup doors carry the export/re-mint round
    trip. Only additive over ``build_keys_bootstrap_stack``; no shared builder is mutated."""
    base = build_keys_bootstrap_stack(res, variants)
    manifest = {**base.manifest}
    manifest["routers_modules"] = [*base.manifest["routers_modules"], "tai42_skeleton.routers.backup"]
    return replace(base, name="keys-bootstrap-backup", manifest=manifest)


@pytest.fixture(scope="module")
def keys_bootstrap_backup_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """A one-worker, keyless bootstrap stack with the backup router mounted (``seed_auth=False``
    — no seeded key, the fresh install the partial-restore recovery scenario starts from)."""
    yield from boot_stack(
        infra, tmp_path_factory.mktemp("keys-bootstrap-backup"), build_keys_bootstrap_backup_stack, seed_auth=False
    )
