"""F3 — combined-op (``apply_env_and_change``) doors over the REAL store.

The ``POST /api/mcp-config/secret-env`` door writes a secret VALUE to the env
store AND an ``!ENV ${KEY}`` MARKER into the manifest as ONE consistent unit, then reloads
the fleet. These doors drive its landed contracts over the agents stack (auth OFF) — the
same real component store the profile/preset doors exercise:

* (a) ATOMIC SUCCESS — env + manifest land together, the manifest carries the marker (no
  plaintext leak), the value is effective, one reload;
* (c) EXPLICIT-KEY COLLISION — an explicit ``key`` colliding with a stored key holding a
  DIFFERENT value is a loud 400 naming the key; nothing written;
* (d) GENERATED-KEY COLLISION mint-fresh — a stored key that collides with the generator's
  first candidate makes the op mint a fresh, non-colliding key; the seeded key's value is
  untouched;
* (f) SECRET MARKS APPENDED — a second op APPENDS its key to ``TAI_ENV_SECRET_KEYS`` read
  from the STORED env, never clobbering the first op's mark.

The remaining F3 combined-op items are authoritative at skeleton-unit level, where the
required fault injection lives (not reproducible over the live file-mode store):
NO-ROLLBACK orphan+report (b) and k8s-409 replay purity (g) need a forced manifest-persist
failure / a fake-K8s 409 harness — ``core/skeleton/tests/config/test_service.py``
(``..._manifest_failure_leaves_orphan_no_rollback``, ``..._k8s_409_replay_writes_env_once``);
dangling-``!ENV`` refusal (e) and the generated-key REGISTERED-shadow avoidance —
``core/skeleton/tests/routers/test_manifest.py`` and ``tests/config/test_boundary.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e.stack import TaiStack

_POINTER = "mcp/0/config/headers/Authorization"
_MARKER_PREFIX = "!ENV ${"


def _marker_key(marker: str) -> str:
    """The env KEY inside an ``!ENV ${KEY}`` marker (the door never returns the key, so its
    NAME is learned from the marker the door wrote)."""
    assert marker.startswith(_MARKER_PREFIX), f"not an !ENV marker: {marker!r}"
    assert marker.endswith("}"), f"not an !ENV marker: {marker!r}"
    return marker[len(_MARKER_PREFIX) : -1]


async def _seed_mcp_entry(stack: TaiStack, title: str) -> None:
    """Replace the mcp section with a single unreachable entry the secret-env door writes a
    marker under (the loopback range is opted into the URL guard by the base env)."""
    await stack.api().post(
        "/api/mcp-config",
        json={"mcp": [{"title": title, "config": {"url": "http://127.0.0.1:1/mcp"}}]},
        retry_on_reloading=True,
    )


async def _authorization_marker(stack: TaiStack) -> str:
    preserved = await stack.api().get("/api/manifest/preserved")
    return preserved["mcp"][0]["config"]["headers"]["Authorization"]


async def test_combined_op_atomic_success(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = agents_stack.api()
    await _seed_mcp_entry(agents_stack, uniq("cop"))

    resp = await api.request_raw(
        "POST",
        "/api/mcp-config/secret-env",
        json={"value": "the-combined-secret", "key_hint": uniq("cop_hint").upper(), "manifest_pointer": _POINTER},
    )
    assert resp.status_code == 200, resp.text

    # The manifest carries the MARKER (no plaintext leak) on the preserved view.
    preserved = await api.get("/api/manifest/preserved")
    key = _marker_key(preserved["mcp"][0]["config"]["headers"]["Authorization"])
    assert "the-combined-secret" not in json.dumps(preserved), "a resolved secret leaked onto the preserved manifest"

    # The env write landed under the generated key, marked secret — both halves are consistent.
    env = (await api.get("/api/config/env"))["env"]
    assert env[key] == "the-combined-secret", f"the env write did not land under {key!r}: {env.get(key)!r}"
    assert key in env.get("TAI_ENV_SECRET_KEYS", "").split(","), "the generated key was not marked secret"


async def test_combined_op_explicit_key_collision_refused(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = agents_stack.api()
    key = uniq("COLLIDE").upper()
    await api.post("/api/config/env", json={key: "live-value"}, retry_on_reloading=True)
    await _seed_mcp_entry(agents_stack, uniq("cop"))

    resp = await api.request_raw(
        "POST",
        "/api/mcp-config/secret-env",
        json={"value": "a-DIFFERENT-value", "key": key, "manifest_pointer": _POINTER},
    )
    assert resp.status_code == 400, resp.text
    assert key in resp.json()["error"], resp.text

    # Nothing written: the live secret stands and the manifest carries no marker.
    env = (await api.get("/api/config/env"))["env"]
    assert env[key] == "live-value", f"a refused collision overwrote the live secret: {env.get(key)!r}"
    preserved = await api.get("/api/manifest/preserved")
    assert "headers" not in preserved["mcp"][0]["config"], "a refused collision still wrote a manifest marker"


async def test_combined_op_generated_key_collision_mints_fresh(
    agents_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = agents_stack.api()
    # A stored key EQUAL to the generator's first candidate (the hint uppercases to itself).
    hint = uniq("COP_SEED").upper()
    await api.post("/api/config/env", json={hint: "seeded-value"}, retry_on_reloading=True)
    await _seed_mcp_entry(agents_stack, uniq("cop"))

    resp = await api.request_raw(
        "POST",
        "/api/mcp-config/secret-env",
        json={"value": "fresh-secret", "key_hint": hint, "manifest_pointer": _POINTER},
    )
    assert resp.status_code == 200, resp.text

    minted = _marker_key(await _authorization_marker(agents_stack))
    assert minted != hint, f"the op reused the colliding key instead of minting fresh: {minted!r}"
    env = (await api.get("/api/config/env"))["env"]
    assert env[hint] == "seeded-value", f"the op clobbered the seeded key: {env.get(hint)!r}"
    assert env[minted] == "fresh-secret", f"the minted key did not carry the new value: {env.get(minted)!r}"


async def test_combined_op_secret_marks_appended_not_clobbered(
    agents_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = agents_stack.api()
    await _seed_mcp_entry(agents_stack, uniq("cop"))

    # First op establishes a mark.
    await api.post(
        "/api/mcp-config/secret-env",
        json={"value": "first-secret", "key_hint": uniq("COP_FIRST").upper(), "manifest_pointer": _POINTER},
        retry_on_reloading=True,
    )
    first_key = _marker_key(await _authorization_marker(agents_stack))

    # Second op APPENDS its key — reading the marks from the STORED env, so the first
    # op's mark survives rather than being clobbered by a stale settings-cache read.
    await api.post(
        "/api/mcp-config/secret-env",
        json={"value": "second-secret", "key_hint": uniq("COP_SECOND").upper(), "manifest_pointer": _POINTER},
        retry_on_reloading=True,
    )
    second_key = _marker_key(await _authorization_marker(agents_stack))
    assert second_key != first_key, "the two ops minted the same key"

    marks = (await api.get("/api/config/env"))["env"].get("TAI_ENV_SECRET_KEYS", "").split(",")
    assert first_key in marks, f"the first op's mark was CLOBBERED by the second: {marks}"
    assert second_key in marks, f"the second op's key was not appended: {marks}"
