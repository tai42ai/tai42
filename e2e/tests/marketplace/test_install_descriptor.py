"""C7 — descriptor-only (``source='spec'``) connector install lifecycle. Opt-in:
collects only with ``TAI_E2E_MARKETPLACE=1``.

iota is a yml-only OAuth connector and kappa a yml-only no-auth (``kind: none``)
connector: neither ships a package, so the registry classifies them ``source='spec'``
(the raw ``tai-plugin.yml`` at the tag is the artifact) and the installer writes only a
manifest ``connectors`` entry — no pip, no venv distribution, no migrations. This module
drives that landed contract end to end:

* iota with ``--env`` only (no ``--secret``): ``GET /api/connectors/providers`` lists it;
  ``CONNECTORS_IOTA_CLIENT_SECRET`` is masked in ``GET /api/config/env`` (derived from the
  live connector, no operator mark); the install carries ``package=null`` /
  ``pip_output=null`` and the row is ``source='spec'`` / ``delivery='descriptor'`` with the
  yml sha256 on the registry version; uninstall drops the provider, leaves the env values,
  keeps the secret masked (now a stored mark); ``tai manifest validate`` with the secret
  unset fails naming it; install with NO env is a 4xx naming BOTH vars (manifest untouched);
  update 0.1.0→0.2.0 surfaces the new scope; the CLI door installs and ``--dry-run`` lists
  ``required_env``.
* kappa with NO env: installs with no env dialog, the provider lists, its
  ``required_env`` is empty, and a connect with user-supplied config launches the managed
  stdio MCP server and reflects the config value back.

It is part of the ``TAI_E2E_MARKETPLACE``-gated suite AND publish-circular skipped: the
spec-source ingest branch lives only on the unpushed marketplace, and the isolated
registry venv resolves tai42-contract from PyPI where the package-optional PluginSpec + the
connector kind + the ``source='spec'`` classification are not yet published, so a
descriptor seed is rejected until ``_MARKETPLACE_PIN`` (marketplace.py) is bumped to the
published descriptor-capable marketplace commit.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import build_marketplace_connectors_stack
from tai42_e2e.marketplace import (
    IOTA_CLIENT_ID_ENV,
    IOTA_CLIENT_SECRET_ENV,
    IOTA_PROVIDER_ID,
    IOTA_REF,
    IOTA_SCOPE_V2_ADDED,
    IOTA_VERSION_V1,
    IOTA_VERSION_V2,
    KAPPA_CONFIG_FIELD_REQUIRED,
    KAPPA_CONFIG_FIELD_SECRET,
    KAPPA_PROVIDER_ID,
    KAPPA_REF,
    MarketplaceService,
    render_iota_descriptor,
    seed_iota_listing,
    seed_kappa_listing,
)
from tai42_e2e.netfixtures import OAuthIdp
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.stack import Infra, TaiStack
from tai42_e2e.waiting import wait_for_async

from ._market_support import cli_env, installed_refs, ok_json, persisted_manifest, run_cli, tai_bin

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skip(
        reason="Publish-circular: the spec-source (source='spec') ingest lives only on the "
        "unpushed marketplace, and the isolated registry venv resolves tai42-contract from PyPI "
        "where the package-optional PluginSpec + connector kind are not yet published. "
        "Un-skipped after _MARKETPLACE_PIN (marketplace.py) is bumped to the descriptor-capable commit."
    ),
]

# The install-time client credentials for iota (the fixture values the install stores). The
# secret is auto-masked from the live connector's client_secret_env even with no --secret.
_IOTA_CLIENT_ID = "e2e-iota-client-id"
_IOTA_CLIENT_SECRET = "e2e-iota-client-secret"


def _manifest_connector_ids(stack: TaiStack) -> set[str]:
    """The provider ids of the persisted manifest's ``connectors`` entries — the rows a
    descriptor install appends and an uninstall removes."""
    entries = persisted_manifest(stack).get("connectors") or []
    return {entry["id"] for entry in entries}


async def _provider_ids(stack: TaiStack) -> set[str]:
    catalog = await stack.api().get("/api/connectors/providers")
    return {p["id"] for p in catalog["providers"]}


async def _provider(stack: TaiStack, provider_id: str) -> dict[str, Any]:
    catalog = await stack.api().get("/api/connectors/providers")
    return {p["id"]: p for p in catalog["providers"]}[provider_id]


async def _env_view(stack: TaiStack) -> dict[str, Any]:
    return await stack.api().get("/api/config/env")


@pytest.fixture
async def descriptor_cleanup(marketplace_connectors_stack: TaiStack) -> AsyncIterator[list[str]]:
    """A per-test uninstall ledger: every descriptor ref appended is best-effort
    uninstalled on teardown, so an aborted spec never leaves a connector in the shared
    stack's manifest to cascade into the next test (a descriptor install touches no venv,
    so the session venv guard cannot catch it)."""
    refs: list[str] = []
    try:
        yield refs
    finally:
        for ref in reversed(refs):
            with contextlib.suppress(Exception):
                await marketplace_connectors_stack.api().post("/api/marketplace/uninstall", json={"ref": ref})


@pytest.fixture(scope="module")
async def iota_seeded(
    marketplace_service: MarketplaceService, package_index: FixturePackageIndex, oauth_idp: OAuthIdp
) -> str:
    """Publish both iota versions (0.1.0, then 0.2.0 adding a scope) into this module's
    registry, rendered against the module's OAuth/MCP stub. Returns the stub base so a
    leg can recompute the exact bytes the registry digested."""
    await seed_iota_listing(marketplace_service, package_index, oauth_idp.base_url)
    return oauth_idp.base_url


async def test_install_iota_with_env_only_masks_secret_and_attributes_spec(
    iota_seeded: str,
    marketplace_connectors_stack: TaiStack,
    marketplace_service: MarketplaceService,
    descriptor_cleanup: list[str],
) -> None:
    stack = marketplace_connectors_stack
    idp_base = iota_seeded

    # Precondition: iota is neither a registered provider nor a manifest connector.
    assert IOTA_PROVIDER_ID not in await _provider_ids(stack)
    assert IOTA_PROVIDER_ID not in _manifest_connector_ids(stack)

    # Install with --env only (both client vars), NO --secret: the secret is auto-masked
    # from the live connector's client_secret_env.
    descriptor_cleanup.append(IOTA_REF)
    result = await stack.api().post(
        "/api/marketplace/install",
        json={
            "ref": IOTA_REF,
            "version": IOTA_VERSION_V1,
            "env": {IOTA_CLIENT_ID_ENV: _IOTA_CLIENT_ID, IOTA_CLIENT_SECRET_ENV: _IOTA_CLIENT_SECRET},
        },
    )
    # A descriptor-only install runs no pip: package and pip_output are null.
    assert result["package"] is None, result
    assert result["pip_output"] is None, result

    # The provider registered from the manifest connectors entry.
    assert IOTA_PROVIDER_ID in await _provider_ids(stack)
    assert IOTA_PROVIDER_ID in _manifest_connector_ids(stack)

    # The secret is masked with NO --secret (derived from client_secret_env); the client
    # id is not masked, and both values are stored verbatim.
    env_view = await _env_view(stack)
    assert IOTA_CLIENT_SECRET_ENV in env_view["secret_keys"], env_view
    assert IOTA_CLIENT_ID_ENV not in env_view["secret_keys"], env_view
    assert env_view["env"][IOTA_CLIENT_ID_ENV] == _IOTA_CLIENT_ID
    assert env_view["env"][IOTA_CLIENT_SECRET_ENV] == _IOTA_CLIENT_SECRET

    # Attribution: source='spec', delivery='descriptor'.
    row = (await installed_refs(stack))[IOTA_REF]
    assert row["source"] == "spec", row
    assert row["delivery"] == "descriptor", row

    # The registry version carries the sha256 of the EXACT rendered bytes it digested.
    versions = (await marketplace_service.api.get(f"/api/v1/plugins/{IOTA_REF}/versions"))["versions"]
    by_version = {v["version"]: v for v in versions}
    expected_sha = hashlib.sha256(render_iota_descriptor(idp_base, IOTA_VERSION_V1).encode("utf-8")).hexdigest()
    assert by_version[IOTA_VERSION_V1]["sha256"] == expected_sha, by_version[IOTA_VERSION_V1]

    # Uninstall: the provider is gone and the manifest connector removed, but the env
    # values are untouched and the secret STAYS masked — its name moved into the stored
    # marks when the connector left the manifest.
    descriptor_cleanup.remove(IOTA_REF)
    await stack.api().post("/api/marketplace/uninstall", json={"ref": IOTA_REF})
    assert IOTA_PROVIDER_ID not in await _provider_ids(stack)
    assert IOTA_PROVIDER_ID not in _manifest_connector_ids(stack)
    assert IOTA_REF not in await installed_refs(stack)

    after = await _env_view(stack)
    assert after["env"][IOTA_CLIENT_ID_ENV] == _IOTA_CLIENT_ID, after
    assert after["env"][IOTA_CLIENT_SECRET_ENV] == _IOTA_CLIENT_SECRET, after
    assert IOTA_CLIENT_SECRET_ENV in after["secret_keys"], after


async def test_manifest_validate_names_the_unset_connector_secret(
    iota_seeded: str,
    marketplace_connectors_stack: TaiStack,
    descriptor_cleanup: list[str],
    tmp_path: Path,
) -> None:
    stack = marketplace_connectors_stack
    descriptor_cleanup.append(IOTA_REF)
    await stack.api().post(
        "/api/marketplace/install",
        json={
            "ref": IOTA_REF,
            "version": IOTA_VERSION_V1,
            "env": {IOTA_CLIENT_ID_ENV: _IOTA_CLIENT_ID, IOTA_CLIENT_SECRET_ENV: _IOTA_CLIENT_SECRET},
        },
    )

    # Dump the connectors block the install wrote to a standalone manifest file.
    connectors = persisted_manifest(stack).get("connectors") or []
    manifest_file = tmp_path / "manifest.yml"
    manifest_file.write_text(yaml.safe_dump({"connectors": connectors}), encoding="utf-8")

    # `tai manifest validate` reads the connector's client_secret_env against the process
    # env; with the secret UNSET (but the id set, isolating the failure to the secret) it
    # exits non-zero naming it — the offline config-pipeline refusal.
    env = cli_env(stack, tmp_path / "home")
    env[IOTA_CLIENT_ID_ENV] = _IOTA_CLIENT_ID
    proc = subprocess.run(
        [tai_bin(), "manifest", "validate", str(manifest_file)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode != 0, proc.stdout
    assert IOTA_CLIENT_SECRET_ENV in (proc.stdout + proc.stderr), proc.stderr


@pytest.fixture
def clean_connectors_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    oauth_idp: OAuthIdp,
) -> Iterator[TaiStack]:
    """A DEDICATED fresh marketplace-connectors stack for the no-env refusal case: its env
    store starts EMPTY (no iota client credentials ever written), so the required-env
    pre-check genuinely fires. It shares the module's registry + package index (iota is
    seeded there), but NOT the module stack's env store — where the earlier legs' installs
    persist the iota creds that a shared stack would let the no-env install resolve."""
    resource_kwargs = {
        "marketplace_url": marketplace_service.base_url,
        "package_index_url": package_index.url,
        "idp_base_url": oauth_idp.base_url,
        "connectors_kek": base64.b64encode(secrets.token_bytes(32)).decode(),
        "connectors_state_hmac_key": base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    yield from boot_stack(
        infra,
        tmp_path_factory.mktemp("marketplace-connectors-clean"),
        build_marketplace_connectors_stack,
        resource_kwargs=resource_kwargs,
    )


async def test_install_without_env_is_4xx_naming_both_vars_and_leaves_manifest(
    iota_seeded: str, clean_connectors_stack: TaiStack
) -> None:
    # A fresh stack with an EMPTY env store: neither CONNECTORS_IOTA_* is supplied, stored,
    # nor in this stack's process env, so ``available`` is empty and the required-env
    # pre-check refuses BEFORE any pip/manifest write (naming both vars). No install
    # happens, so there is nothing to clean up — and the update leg is not cascaded.
    stack = clean_connectors_stack
    before = _manifest_connector_ids(stack)
    assert IOTA_PROVIDER_ID not in before

    resp = await stack.api().request_raw(
        "POST", "/api/marketplace/install", json={"ref": IOTA_REF, "version": IOTA_VERSION_V1}
    )
    assert 400 <= resp.status_code < 500, resp.text
    body = resp.text
    assert IOTA_CLIENT_ID_ENV in body, body
    assert IOTA_CLIENT_SECRET_ENV in body, body

    # The refusal is before any manifest write: iota is not a connector and not installed.
    assert IOTA_PROVIDER_ID not in _manifest_connector_ids(stack)
    assert _manifest_connector_ids(stack) == before
    assert IOTA_REF not in await installed_refs(stack)


async def test_update_iota_surfaces_the_new_scope(
    iota_seeded: str, marketplace_connectors_stack: TaiStack, descriptor_cleanup: list[str]
) -> None:
    stack = marketplace_connectors_stack
    env = {IOTA_CLIENT_ID_ENV: _IOTA_CLIENT_ID, IOTA_CLIENT_SECRET_ENV: _IOTA_CLIENT_SECRET}

    descriptor_cleanup.append(IOTA_REF)
    await stack.api().post("/api/marketplace/install", json={"ref": IOTA_REF, "version": IOTA_VERSION_V1, "env": env})
    provider = await _provider(stack, IOTA_PROVIDER_ID)
    scopes = provider["sub_services"][0]["scopes"]
    assert IOTA_SCOPE_V2_ADDED not in scopes, scopes

    # Update to 0.2.0: the new scope becomes visible on the registered provider (the
    # connector descriptor was replaced in the manifest and re-registered on reload).
    await stack.api().post("/api/marketplace/update", json={"ref": IOTA_REF, "version": IOTA_VERSION_V2})
    row = (await installed_refs(stack))[IOTA_REF]
    assert row["version"] == IOTA_VERSION_V2, row
    updated = await _provider(stack, IOTA_PROVIDER_ID)
    assert IOTA_SCOPE_V2_ADDED in updated["sub_services"][0]["scopes"], updated


async def test_cli_install_and_dry_run(
    iota_seeded: str, marketplace_connectors_stack: TaiStack, descriptor_cleanup: list[str], tmp_path: Path
) -> None:
    stack = marketplace_connectors_stack
    env = cli_env(stack, tmp_path / "home")

    # --dry-run lists the server-computed required_env ({name, secret}) without installing.
    dry = ok_json(run_cli(env, "install", IOTA_REF, "--version", IOTA_VERSION_V1, "--dry-run"))
    required = {r["name"]: r["secret"] for r in dry["required_env"]}
    assert required == {IOTA_CLIENT_ID_ENV: False, IOTA_CLIENT_SECRET_ENV: True}, dry
    assert dry["delivery"] == "descriptor", dry
    assert IOTA_PROVIDER_ID not in _manifest_connector_ids(stack)

    # The CLI door installs by ref with --env + --secret.
    descriptor_cleanup.append(IOTA_REF)
    ok_json(
        run_cli(
            env,
            "install",
            IOTA_REF,
            "--version",
            IOTA_VERSION_V1,
            "--env",
            f"{IOTA_CLIENT_ID_ENV}={_IOTA_CLIENT_ID}",
            "--env",
            f"{IOTA_CLIENT_SECRET_ENV}={_IOTA_CLIENT_SECRET}",
            "--secret",
            IOTA_CLIENT_SECRET_ENV,
        )
    )
    assert IOTA_PROVIDER_ID in await _provider_ids(stack)


# ---- kappa: no-auth descriptor, no env dialog, connects with config ----------


@pytest.fixture(scope="module")
async def kappa_seeded(marketplace_service: MarketplaceService, package_index: FixturePackageIndex) -> None:
    """Publish the kappa no-auth connector descriptor into this module's registry,
    launching the managed stdio MCP server with the SUT interpreter."""
    await seed_kappa_listing(marketplace_service, package_index, sys.executable)


async def test_kappa_installs_with_no_env_and_connects_with_config(
    kappa_seeded: None, marketplace_connectors_stack: TaiStack, descriptor_cleanup: list[str]
) -> None:
    stack = marketplace_connectors_stack

    # No-auth descriptor: the install preview reports no required env.
    preview = await stack.api().post("/api/marketplace/install/preview", json={"ref": KAPPA_REF})
    assert preview["required_env"] == [], preview
    assert preview["delivery"] == "descriptor", preview

    # Installs with NO env dialog (no env body at all) and lists as a provider.
    descriptor_cleanup.append(KAPPA_REF)
    result = await stack.api().post("/api/marketplace/install", json={"ref": KAPPA_REF})
    assert result["package"] is None, result
    assert result["pip_output"] is None, result
    assert KAPPA_PROVIDER_ID in await _provider_ids(stack)

    # Connect with user-supplied config: the values inject on the managed server's stdio
    # env, and its reflect_env tool reads one back — proving the no-auth config path.
    alpha_value = "kappa-alpha-value"
    start = await stack.api().post(
        "/api/connectors/connections/start",
        json={
            "provider_id": KAPPA_PROVIDER_ID,
            "alias": "k",
            "enabled_sub_services": ["default"],
            "config_values": {KAPPA_CONFIG_FIELD_REQUIRED: alpha_value, KAPPA_CONFIG_FIELD_SECRET: "kappa-beta-value"},
        },
    )
    entry_title = start["added_manifest_entries"][0]
    reflect_tool = f"{entry_title}_reflect_env"

    async def reflects() -> bool:
        async with stack.mcp() as mcp:
            if reflect_tool not in await mcp.tool_names():
                return False
            result = await mcp.call_tool(reflect_tool, {"key": KAPPA_CONFIG_FIELD_REQUIRED}, raise_on_error=False)
        if result.is_error:
            return False
        payload = result.data if result.data is not None else result.structured_content
        return isinstance(payload, dict) and payload.get("value") == alpha_value

    await wait_for_async(
        reflects, deadline=45.0, message=f"kappa managed tool {reflect_tool!r} never reflected the injected config"
    )

    # Disconnect leaves no trace of the connection's managed entry.
    await stack.api().delete(f"/api/connectors/connections/{start['connection_id']}")
