"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

The registry read API (faceted search, listing detail, versions, categories) and
the skeleton's server-side ``/api/marketplace/search`` proxy, over the seeded
alpha/beta/gamma catalog. Facets are asserted as exact membership ("these refs
and no others") so a mis-faceted row fails loudly.
"""

from __future__ import annotations

from typing import Any

import pytest

from tai42_e2e.marketplace import (
    ALPHA_REF,
    BETA_REF,
    GAMMA_REF,
    MarketplaceService,
    contract_facet_probe_versions,
)
from tai42_e2e.stack import TaiStack

# The marketplace stack runs no backend worker; skip on non-default backend legs.
pytestmark = pytest.mark.backendless


def _listings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    listings = payload["listings"]
    assert isinstance(listings, list)
    return listings


def _refs(payload: dict[str, Any]) -> set[str]:
    return {row["ref"] for row in _listings(payload)}


async def test_seeded_catalog_is_browsable(marketplace_service: MarketplaceService) -> None:
    api = marketplace_service.api
    payload = await api.get("/api/v1/search")
    assert _refs(payload) == {ALPHA_REF, BETA_REF, GAMMA_REF}


async def test_full_text_query_matches_probes(marketplace_service: MarketplaceService) -> None:
    payload = await marketplace_service.api.get("/api/v1/search?q=probe")
    refs = _refs(payload)
    assert ALPHA_REF in refs
    assert BETA_REF in refs


async def test_kind_facet(marketplace_service: MarketplaceService) -> None:
    api = marketplace_service.api

    # kind= selects LISTINGS carrying at least one item of that kind, grouped or
    # not. Only beta ships an extension item, but beta groups BOTH its items (a
    # tool and an extension) under one family, so its kinds array — which
    # summarizes UNGROUPED items only — is empty and the family surfaces in groups.
    extensions = _listings(await api.get("/api/v1/search?kind=extension"))
    assert len(extensions) == 1
    assert extensions[0]["ref"] == BETA_REF
    assert extensions[0]["kinds"] == []
    assert extensions[0]["groups"] == [{"name": "probe-suite", "count": 2}]

    tools = _listings(await api.get("/api/v1/search?kind=tool"))
    assert {row["ref"] for row in tools} == {ALPHA_REF, BETA_REF, GAMMA_REF}
    assert len(tools) == 3
    by_ref = {row["ref"]: row for row in tools}
    # alpha and gamma surface their tool ungrouped, so it appears in kinds with
    # its name; beta's tool is grouped, so it appears only in the group count.
    assert by_ref[ALPHA_REF]["kinds"] == [{"kind": "tool", "count": 1, "names": ["e2e_market_probe"]}]
    assert by_ref[GAMMA_REF]["kinds"] == [{"kind": "tool", "count": 1, "names": ["e2e_market_gamma_probe"]}]
    assert by_ref[BETA_REF]["kinds"] == []
    assert by_ref[BETA_REF]["groups"] == [{"name": "probe-suite", "count": 2}]


async def test_kinds_aggregation_over_latest_version(marketplace_service: MarketplaceService) -> None:
    # Each row summarizes the listing's latest published version two ways: kinds
    # counts only its UNGROUPED items, each with its item names ASC, ordered
    # count DESC then kind ASC; groups counts its logical families, count DESC
    # then name ASC. Beta's latest ships two items both under one group, so its
    # kinds is empty and its group counts two; alpha's and gamma's each ship one
    # ungrouped tool and no groups.
    listings = {row["ref"]: row for row in _listings(await marketplace_service.api.get("/api/v1/search"))}
    assert listings[BETA_REF]["kinds"] == []
    assert listings[BETA_REF]["groups"] == [{"name": "probe-suite", "count": 2}]
    assert listings[ALPHA_REF]["kinds"] == [{"kind": "tool", "count": 1, "names": ["e2e_market_probe"]}]
    assert listings[ALPHA_REF]["groups"] == []
    assert listings[GAMMA_REF]["kinds"] == [{"kind": "tool", "count": 1, "names": ["e2e_market_gamma_probe"]}]
    assert listings[GAMMA_REF]["groups"] == []


async def test_tags_facet(marketplace_service: MarketplaceService) -> None:
    payload = await marketplace_service.api.get("/api/v1/search?tags=alpha")
    assert _refs(payload) == {ALPHA_REF}


async def test_category_facet(marketplace_service: MarketplaceService) -> None:
    # alpha's category is 'utilities', distinct from every other seeded listing.
    payload = await marketplace_service.api.get("/api/v1/search?category=utilities")
    assert _refs(payload) == {ALPHA_REF}


async def test_sort_name_orders_by_listing_name(marketplace_service: MarketplaceService) -> None:
    # sort=name orders rows by listing name ASC. The seeded listing names
    # (e2e-alpha, e2e-beta, e2e-gamma) sort to the alpha, beta, gamma order.
    listings = _listings(await marketplace_service.api.get("/api/v1/search?sort=name"))
    assert [row["ref"] for row in listings] == [ALPHA_REF, BETA_REF, GAMMA_REF]


async def test_sort_downloads_returns_complete_set(marketplace_service: MarketplaceService) -> None:
    # Order is registry-internal policy; only completeness is asserted here.
    payload = await marketplace_service.api.get("/api/v1/search?sort=downloads")
    assert _refs(payload) == {ALPHA_REF, BETA_REF, GAMMA_REF}
    assert payload["total"] == len(_listings(payload))


async def test_contract_facet(marketplace_service: MarketplaceService) -> None:
    api = marketplace_service.api
    # The forge stamps every fixture's contract range to the workspace band, so the
    # facet probes are derived from that same band (inside it → all three; below
    # its lower bound → none) to stay correct across release-version windows.
    inside_version, below_version = contract_facet_probe_versions()
    inside = _listings(await api.get(f"/api/v1/search?contract={inside_version}"))
    assert {row["ref"] for row in inside} == {ALPHA_REF, BETA_REF, GAMMA_REF}

    outside = _listings(await api.get(f"/api/v1/search?contract={below_version}"))
    assert outside == []


async def test_listing_detail_and_versions(marketplace_service: MarketplaceService) -> None:
    api = marketplace_service.api
    detail = await api.get(f"/api/v1/plugins/{ALPHA_REF}")
    assert detail["ref"] == ALPHA_REF
    assert detail["latest"]["version"] == "0.2.0"
    assert detail["latest"]["status"] == "published"
    assert detail["latest"]["items"], "latest version should carry its items"
    # alpha ships no icon or display name, so the read falls back to the monogram
    # (icon_url null) and the spec license surfaces on the detail payload.
    assert detail["icon_url"] is None
    assert detail["display_name"] is None
    assert detail["license"] == "Apache-2.0"

    versions = (await api.get(f"/api/v1/plugins/{ALPHA_REF}/versions"))["versions"]
    by_version = {row["version"]: row["status"] for row in versions}
    assert by_version == {"0.1.0": "published", "0.2.0": "published"}


async def test_categories_vocabulary(marketplace_service: MarketplaceService) -> None:
    categories = (await marketplace_service.api.get("/api/v1/categories"))["categories"]
    for expected in ("utilities", "productivity", "execution", "webhooks"):
        assert expected in categories


async def test_unknown_ref_is_404_naming_the_ref(marketplace_service: MarketplaceService) -> None:
    body = await marketplace_service.api.get("/api/v1/plugins/tai42/nope", expect=404)
    assert "tai42/nope" in body["error"]


async def test_skeleton_proxy_parity(marketplace_service: MarketplaceService, marketplace_stack: TaiStack) -> None:
    direct = _refs(await marketplace_service.api.get("/api/v1/search?q=probe"))
    proxied = _refs(await marketplace_stack.api().get("/api/marketplace/search?q=probe"))
    assert proxied == direct
