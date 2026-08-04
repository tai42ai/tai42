"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

The signed GitHub-webhook ingest path, which no poller spec drives. Delta is the
one github-sourced fixture (no wheel, never on the PyPI handlers): it is seeded
repo-form and ingested over the github-shaped surfaces, and a second version is
ingested by a correctly-signed tag-push webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from tai42_e2e.marketplace import (
    DELTA_REF,
    DELTA_REPOSITORY_URL,
    FixtureArtifacts,
    MarketplaceService,
)
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

pytestmark = [
    pytest.mark.backendless,
    # The github-ingest path here reads the in-process FixturePackageIndex (the release
    # surfaces it serves + the source=github ingest). Under TAI_E2E_REAL=marketplace-github the
    # ingest goes to real github, so this is the github-ingest mock leg — it steps aside. The
    # marketplace-pypi seam is handled suite-wide in conftest (it breaks the shared fixture seed
    # for EVERY module, not just this one), so it is deliberately NOT repeated here. Inert while
    # TAI_E2E_REAL is empty (and the module is opt-in via TAI_E2E_MARKETPLACE=1 regardless).
    pytest.mark.skipif(
        HarnessSettings().is_real("marketplace-github"),
        reason="FixturePackageIndex github ingest is the marketplace-github mock leg; real on creds host (PLAN_2 §F)",
    ),
]

_REPO_FULL_NAME = "tai42ai/tai-e2e-market-delta"


async def _wait_published(mp: MarketplaceService, version: str, *, deadline: float = 30.0) -> None:
    async def published() -> bool:
        rows = (await mp.api.get(f"/api/v1/plugins/{DELTA_REF}/versions"))["versions"]
        statuses = {row["version"]: row["status"] for row in rows}
        if statuses.get(version) == "scan_failed":
            raise AssertionError(f"delta {version} is scan_failed (ingest validation failed): {statuses}")
        return statuses.get(version) == "published"

    await wait_for_async(published, deadline=deadline, message=f"delta {version} never reached status 'published'")


async def test_signed_webhook_ingests_github_source(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
) -> None:
    mp = marketplace_service

    # Stage delta 0.1.0 on the GITHUB surfaces only (no wheel, never on /pypi/), then
    # seed repo-form: source-detection sees the 404 on pypi and ingests over github.
    # The seed entry carries the inline stamped 0.1.0 spec and the tag it installs
    # from; the tag webhook fetches later versions' specs over the contents surface.
    package_index.register_github_release("v0.1.0", fixture_artifacts.delta_v1.plugin_yml)
    seeded = await mp.api.post(
        "/api/v1/admin/seed",
        json={
            "repos": [
                {
                    "repo": DELTA_REPOSITORY_URL,
                    "plugin_yml": fixture_artifacts.delta_v1.plugin_yml,
                    "tag": "v0.1.0",
                }
            ]
        },
        headers=mp.admin_headers,
    )
    # Assert the seed row published: a failed row must surface here, not as a lost
    # 30 s publish timeout with its root cause buried in the registry log.
    assert seeded["results"][0]["status"] == "published", seeded
    await _wait_published(mp, "0.1.0")

    # The ingested tag rides the resolve response only (never browse/detail/versions),
    # and source is github — delta got no wheel, so source-detection's pypi probe
    # 404'd and the real github ingest ran.
    resolved = await mp.api.post(f"/api/v1/plugins/{DELTA_REF}/resolve", json={})
    assert resolved["tag"] == "v0.1.0"
    assert resolved["source"] == "github"

    # Delta got no wheel: source-detection probed the pypi handler (which 404'd,
    # since nothing was registered there) and fell through to the github ingest.
    assert package_index.requests.count("/pypi/tai-e2e-market-delta/json") >= 1

    # Stage 0.2.0 and drive it in with a correctly-signed tag-push webhook. The
    # webhook fetches the spec at ``?ref=v0.2.0``, so the tag serves its own
    # stamped 0.2.0 spec (a version mismatch would be rejected).
    package_index.register_github_release("v0.2.0", fixture_artifacts.delta_v2.plugin_yml)
    body = {
        "ref": "refs/tags/v0.2.0",
        "repository": {"html_url": DELTA_REPOSITORY_URL, "full_name": _REPO_FULL_NAME},
    }
    raw = json.dumps(body).encode()
    signature = hmac.new(mp.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    response = await mp.api.request_raw(
        "POST",
        "/api/v1/webhooks/github",
        content=raw,
        headers={"X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Event": "push"},
    )
    assert response.status_code in (200, 202), f"webhook rejected: {response.status_code} {response.text}"

    await _wait_published(mp, "0.2.0")
    detail = await mp.api.get(f"/api/v1/plugins/{DELTA_REF}")
    assert detail["latest"]["version"] == "0.2.0"
