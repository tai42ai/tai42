"""F3 — the settings-profile polish doors over the REAL versioned store.

Runs against the agents stack (auth OFF), which carries the skeleton component store the
profile documents persist in — the same store the preset doors exercise. The apply
pipeline itself is certified by ``test_flip``; this suite drives the CRUD / versioning /
diff / refusal contracts the doors declare, plus the resolved-manifest no-leak posture
(``GET /api/manifest`` serves the PRESERVED view after PLAN_3's retighten).

The action-class fence each profile route declares (list ``read``; get/version/diff
``secret``; put/delete/rollback/apply ``fenced``) is certified over an auth-ON stack by
``access_control/test_action_class_surface.py`` — the sanctioned action-class surface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.stack import TaiStack

# A boot-identity X-band key no profile may carry (``config.boundary.X_BAND_EXTRA``).
_X_BAND_KEY = "TAI_RUN_MODE"


async def _put(stack: TaiStack, name: str, body: dict[str, Any]) -> Any:
    return await stack.api().put(f"/api/config/profiles/{name}", json=body, retry_on_reloading=True)


async def test_crud_reveals_body_and_list_masks(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = agents_stack.api()
    name = uniq("profile")
    body = {
        "description": "a probe profile",
        "env": {"E2E_P_KEY": "v1", "E2E_P_SECRET": "s3cr3t"},
        "secret_keys": ["E2E_P_SECRET"],
    }

    # A create returns ONLY {ok, version} — never the body, so a secret never re-emits on
    # the write path.
    created = await _put(agents_stack, name, body)
    assert created == {"ok": True, "version": 1}, created

    # The body door round-trips REAL env values (masking is display-side only, never on
    # the wire) — the ``secret``-fenced read.
    got = await api.get(f"/api/config/profiles/{name}")
    assert got == {"description": "a probe profile", "env": body["env"], "secret_keys": ["E2E_P_SECRET"]}

    # The list door carries NO env values — it masks by omission, one {name, description}
    # row per profile, excluding the reserved ``@``-prefixed snapshots.
    rows = await api.get("/api/config/profiles")
    row = next(r for r in rows if r["name"] == name)
    assert row == {"name": name, "description": "a probe profile"}
    assert all(not r["name"].startswith("@") for r in rows), f"a reserved @-profile leaked into the list: {rows}"

    # Soft delete keeps history but the active body 404s.
    assert await api.delete(f"/api/config/profiles/{name}") == {"ok": True}
    gone = await api.request_raw("GET", f"/api/config/profiles/{name}")
    assert gone.status_code == 404, gone.text


async def test_reserved_name_refused(agents_stack: TaiStack) -> None:
    resp = await agents_stack.api().request_raw(
        "PUT", "/api/config/profiles/@nope", json={"description": "", "env": {}, "secret_keys": []}
    )
    assert resp.status_code == 400, resp.text
    assert "@nope" in resp.json()["error"]


async def test_versions_and_rollback(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = agents_stack.api()
    name = uniq("profile")
    await _put(agents_stack, name, {"description": "v1", "env": {"E2E_V": "one"}, "secret_keys": []})
    second = await _put(agents_stack, name, {"description": "v2", "env": {"E2E_V": "two"}, "secret_keys": []})
    assert second == {"ok": True, "version": 2}

    versions = await api.get(f"/api/config/profiles/{name}/versions")
    by_num = {v["version"]: v for v in versions}
    assert set(by_num) == {1, 2}, versions
    assert by_num[2]["is_current"] is True, versions
    assert by_num[1]["is_current"] is False, versions

    # A version body door reveals the REAL body (secret-fenced), immutable across the tag/
    # rollback annotations.
    v1_body = await api.get(f"/api/config/profiles/{name}/versions/1")
    assert v1_body["body"]["env"] == {"E2E_V": "one"}

    # Rollback re-points the active version (no data copy); the active body follows.
    rolled = await api.post(f"/api/config/profiles/{name}/rollback", json={"version": 1})
    assert rolled == {"ok": True, "version": 1}
    assert (await api.get(f"/api/config/profiles/{name}"))["env"] == {"E2E_V": "one"}

    # A non-integer version segment is a clean 400; an unknown version a 404.
    bad = await api.request_raw("GET", f"/api/config/profiles/{name}/versions/notanint")
    assert bad.status_code == 400, bad.text
    missing = await api.request_raw("GET", f"/api/config/profiles/{name}/versions/99")
    assert missing.status_code == 404, missing.text


async def test_diff_buckets(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = agents_stack.api()
    # Seed known stored-env keys (merge), then a profile that adds/removes/changes against
    # them. Membership assertions tolerate other stored keys the shared stack carries.
    await api.post(
        "/api/config/env",
        json={"E2E_DIFF_KEEP": "same", "E2E_DIFF_CHANGE": "old", "E2E_DIFF_GONE": "x"},
        retry_on_reloading=True,
    )
    name = uniq("profile")
    await _put(
        agents_stack,
        name,
        {
            "description": "",
            "env": {"E2E_DIFF_KEEP": "same", "E2E_DIFF_CHANGE": "new", "E2E_DIFF_ADD": "y"},
            "secret_keys": [],
        },
    )

    diff = await api.post(f"/api/config/profiles/{name}/diff")
    assert "E2E_DIFF_ADD" in diff["added"], diff
    assert "E2E_DIFF_GONE" in diff["removed"], diff
    assert {"key": "E2E_DIFF_CHANGE", "old": "old", "new": "new"} in diff["changed"], diff
    assert "E2E_DIFF_KEEP" not in diff["added"], diff
    assert "E2E_DIFF_KEEP" not in diff["removed"], diff
    # The recycle-classification buckets are always present lists (empty for an all-hot diff).
    assert isinstance(diff["recycle_keys"], list), diff
    assert isinstance(diff["refused_keys"], list), diff


async def test_x_band_key_refused(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    name = uniq("profile")
    resp = await agents_stack.api().request_raw(
        "PUT", f"/api/config/profiles/{name}", json={"description": "", "env": {_X_BAND_KEY: "prod"}, "secret_keys": []}
    )
    assert resp.status_code == 400, resp.text
    assert _X_BAND_KEY in resp.json()["error"]
    # A refused save never persists.
    assert (await agents_stack.api().request_raw("GET", f"/api/config/profiles/{name}")).status_code == 404


async def test_key_material_key_refused(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """A profile that CHANGES a ``key_material`` field is refused DISTINCTLY from the X-band
    rule — a key_material field may be ``hot`` (not X-band) yet its VALUE must never be
    rotated through a profile; the refusal names the key and points at out-of-band rotation.
    (An UNCHANGED carry is allowed — certified at the unit level; here we drive the CHANGE
    case.) The probe fixture registers ``E2E_PROBE_SECRET_KEY_MATERIAL`` on every leg so this
    always has a target."""
    api = agents_stack.api()
    schema = await api.get("/api/config/settings-schema")
    key_material_vars = [
        field["env_var"]
        for group in schema["groups"]
        for field in group["fields"]
        if field.get("key_material") and field["env_var"]
    ]
    assert key_material_vars, (
        f"no key_material field is registered on this leg: {[g['name'] for g in schema['groups']]}"
    )
    target = key_material_vars[0]

    # A profile round-tripped from the stored env but CHANGING the key-material key to a new
    # value — the rotation-via-profile the refusal guards against.
    stored = (await api.get("/api/config/env"))["env"]
    changed_value = uniq("rotated-kek")
    assert changed_value != stored.get(target), "the test value must differ from the current stored value"
    name = uniq("profile")
    resp = await api.request_raw(
        "PUT",
        f"/api/config/profiles/{name}",
        json={"description": "", "env": {**stored, target: changed_value}, "secret_keys": []},
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert target in error, error
    assert "rotat" in error.lower(), f"the key_material refusal must point at the rotation path: {error}"
    assert (await api.request_raw("GET", f"/api/config/profiles/{name}")).status_code == 404


async def test_manifest_preserved_posture(agents_stack: TaiStack) -> None:
    """After PLAN_3's retighten, ``GET /api/manifest`` serves the PRESERVED ``{mcp,
    user_tools}`` view (``!ENV`` markers intact, no resolved plaintext) — the SAME view the
    explicit ``/api/manifest/preserved`` door reads."""
    api = agents_stack.api()
    manifest = await api.get("/api/manifest")
    preserved = await api.get("/api/manifest/preserved")
    assert manifest == preserved, f"the manifest read is not the preserved view: {manifest} != {preserved}"
    assert isinstance(manifest["mcp"], list), manifest
    assert isinstance(manifest["user_tools"], list), manifest
    # No resolved ``!ENV`` marker leaks: every marker leaf under the preserved view still
    # begins with the ``!ENV`` sentinel (vacuously true for a manifest with no mcp secrets).
    for entry in manifest["mcp"]:
        for value in _string_leaves(entry):
            assert "!ENV" in value or "${" not in value, f"a resolved marker leaked onto the preserved view: {value!r}"


@pytest.mark.timeout(300)
async def test_previous_revert_round_trip(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """Applying a profile snapshots the pre-apply stored env into the reserved ``@previous``;
    applying ``@previous`` reverts the whole band — the D15 rollback anchor."""
    api = agents_stack.api()
    before = (await api.get("/api/config/env"))["env"]
    marker = uniq("E2E_PREV").upper()
    assert marker not in before

    name = uniq("profile")
    await _put(agents_stack, name, {"description": "", "env": {**before, marker: "on"}, "secret_keys": []})
    await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True)
    assert marker in (await api.get("/api/config/env"))["env"], "the applied profile did not land in the stored env"

    # @previous holds the pre-apply band; applying it reverts the marker away.
    await api.post("/api/config/profiles/@previous/apply", retry_on_reloading=True)
    assert marker not in (await api.get("/api/config/env"))["env"], "applying @previous did not revert the stored env"


def _string_leaves(node: Any) -> list[str]:
    """Every string scalar leaf of ``node`` (descending maps and lists)."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in _string_leaves(value)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in _string_leaves(value)]
    return []
