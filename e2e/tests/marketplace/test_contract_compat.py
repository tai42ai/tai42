"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

The registry side of the plugin-compat mechanism, over the zeta compat listing
(two published versions with different declared contract ranges): the core-aware
resolve — ``contract=<version>`` pins the newest COMPATIBLE published version and
a no-compatible-version pin is a typed 4xx naming the newest version and its
range — and the ingest lockstep gate, which rejects a wheel whose packaged
``tai-plugin.yml`` contract range disagrees with its built ``Requires-Dist``
tai42-contract specifier.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest
from _market_support import resolve_path

from tai42_e2e.marketplace import (
    ZETA_COMPAT_VERSION,
    ZETA_INCOMPAT_VERSION,
    ZETA_NARROW_CONTRACT_RANGE,
    ZETA_PACKAGE,
    ZETA_REF,
    ZETA_WIDE_CONTRACT_RANGE,
    MarketplaceService,
    forge_zeta_wheel,
)
from tai42_e2e.pkgsource import BuiltWheel, FixturePackageIndex

# The marketplace registry specs boot no skeleton stack and no backend worker;
# skip on non-default backend legs.
pytestmark = pytest.mark.backendless


async def test_resolve_pins_newest_contract_compatible_version(
    marketplace_service: MarketplaceService, zeta_catalog: tuple[BuiltWheel, BuiltWheel]
) -> None:
    api = marketplace_service.api

    # The running core's contract version is compatible only with 0.1.0's wide
    # range, so the newest published version (0.2.0, narrow future range) is
    # passed over in favor of the newest COMPATIBLE one.
    running = importlib.metadata.version("tai42-contract")
    resolved = await api.post(resolve_path(ZETA_REF, contract=running))
    assert resolved["version"] == ZETA_COMPAT_VERSION
    assert resolved["contract_range"] == ZETA_WIDE_CONTRACT_RANGE
    assert resolved["package"] == ZETA_PACKAGE

    # A core inside the narrow future range pins the newest version — the same
    # listing resolves differently per caller contract, which is the mechanism.
    resolved = await api.post(resolve_path(ZETA_REF, contract="9.5.0"))
    assert resolved["version"] == ZETA_INCOMPAT_VERSION
    assert resolved["contract_range"] == ZETA_NARROW_CONTRACT_RANGE


async def test_resolve_refuses_when_no_version_is_compatible(
    marketplace_service: MarketplaceService, zeta_catalog: tuple[BuiltWheel, BuiltWheel]
) -> None:
    # A contract version beyond every declared range: the refusal is a typed
    # 409 (the listing and its versions exist; every declared range refuses this
    # pin) whose message names the newest published version and its range, so a
    # caller can see exactly how far ahead (or behind) its core is.
    response = await marketplace_service.api.request_raw("POST", resolve_path(ZETA_REF, contract="12.0.0"))
    assert response.status_code == 409, f"expected a 409 refusal, got {response.status_code}: {response.text}"
    body = response.json()
    message = body["error"]
    assert isinstance(message, str)
    assert ZETA_INCOMPAT_VERSION in message, f"refusal does not name the newest version: {message!r}"
    assert ZETA_NARROW_CONTRACT_RANGE in message, f"refusal does not name the required range: {message!r}"


async def test_seed_rejects_contract_requires_dist_mismatch(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    zeta_catalog: tuple[BuiltWheel, BuiltWheel],
    tmp_path: Path,
) -> None:
    mp = marketplace_service

    # A 0.3.0 wheel whose packaged tai-plugin.yml declares the wide range while
    # its built Requires-Dist pins the narrow one — the lockstep violation.
    mismatched = forge_zeta_wheel(
        "0.3.0",
        tmp_path,
        contract_range=ZETA_WIDE_CONTRACT_RANGE,
        requires_dist_range=ZETA_NARROW_CONTRACT_RANGE,
    )
    package_index.register(mismatched)

    seeded = await mp.api.post(
        "/api/v1/admin/seed",
        json={"repos": [{"package": ZETA_PACKAGE, "plugin_yml": mismatched.plugin_yml}]},
        headers=mp.admin_headers,
    )
    assert seeded["failed"] == 1
    rows = seeded["results"]
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    error = row["error"]
    assert isinstance(error, str)
    assert "contract" in error.lower(), f"rejection does not name the contract mismatch: {error!r}"

    # The rejection held: 0.3.0 never published, so the listing still resolves
    # its previously newest published version.
    versions = await mp.api.get(f"/api/v1/plugins/{ZETA_REF}/versions")
    statuses = {version_row["version"]: version_row["status"] for version_row in versions["versions"]}
    assert statuses.get("0.3.0") != "published"
    resolved = await mp.api.post(resolve_path(ZETA_REF))
    assert resolved["version"] == ZETA_INCOMPAT_VERSION
