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

from tai42_e2e.marketplace import ALPHA_REF, BETA_REF, GAMMA_REF, MarketplaceService
from tai42_e2e.stack import TaiStack

# The marketplace stack runs no backend worker; skip on non-default backend legs.
pytestmark = pytest.mark.backendless


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload["items"]
    assert isinstance(items, list)
    return items


def _refs(payload: dict[str, Any]) -> set[str]:
    return {row["ref"] for row in _items(payload)}


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

    extensions = _items(await api.get("/api/v1/search?kind=extension"))
    assert len(extensions) == 1
    assert extensions[0]["item"]["kind"] == "extension"
    assert extensions[0]["ref"] == BETA_REF

    tools = _items(await api.get("/api/v1/search?kind=tool"))
    assert {row["ref"] for row in tools} == {ALPHA_REF, BETA_REF, GAMMA_REF}
    assert all(row["item"]["kind"] == "tool" for row in tools)
    assert len(tools) == 3


async def test_tags_facet(marketplace_service: MarketplaceService) -> None:
    payload = await marketplace_service.api.get("/api/v1/search?tags=alpha")
    assert _refs(payload) == {ALPHA_REF}


async def test_category_facet(marketplace_service: MarketplaceService) -> None:
    # alpha's category is 'utilities', distinct from every other seeded listing.
    payload = await marketplace_service.api.get("/api/v1/search?category=utilities")
    assert _refs(payload) == {ALPHA_REF}


async def test_sort_name_groups_items_by_listing(marketplace_service: MarketplaceService) -> None:
    # sort=name orders by listing name, then item name (l.name ASC, i.name ASC,
    # i.id ASC), so one listing's items stay contiguous. For the seeded catalog
    # that is alpha's item, then beta's two (beta_marker < e2e_market_beta_probe),
    # then gamma's — NOT the globally item-name-sorted order.
    items = _items(await marketplace_service.api.get("/api/v1/search?sort=name"))
    names = [row["item"]["name"] for row in items]
    assert names == [
        "e2e_market_probe",
        "beta_marker",
        "e2e_market_beta_probe",
        "e2e_market_gamma_probe",
    ]


async def test_sort_downloads_returns_complete_set(marketplace_service: MarketplaceService) -> None:
    # Order is registry-internal policy; only completeness is asserted here.
    payload = await marketplace_service.api.get("/api/v1/search?sort=downloads")
    assert _refs(payload) == {ALPHA_REF, BETA_REF, GAMMA_REF}
    assert payload["total"] == len(_items(payload))


async def test_contract_facet(marketplace_service: MarketplaceService) -> None:
    api = marketplace_service.api
    # Every fixture declares contract ">=0.3,<0.4".
    inside = _items(await api.get("/api/v1/search?contract=0.3.5"))
    assert {row["ref"] for row in inside} == {ALPHA_REF, BETA_REF, GAMMA_REF}

    outside = _items(await api.get("/api/v1/search?contract=0.2.5"))
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
