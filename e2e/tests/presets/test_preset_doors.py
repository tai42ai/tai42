"""C7 — the presets polish doors over the REAL versioning store: the referees
preflight, the dry-run validate door (create + version modes), and the
version-tags annotation door.

Runs against the agents stack: it carries both a plain tool (``e2e_echo``) and a
registered agent (``tools_agent``), so a preset can compose another preset as a
tool — the reference a referee lookup and a rename preflight are about.

Inducing a live quarantine (to drive ``conflicted_reason`` non-null) needs a base
tool to vanish across a reload — manifest surgery the harness cannot do cleanly —
so these tests assert the healthy, non-conflicted shape instead: ``conflicted:
false`` / ``conflicted_reason: null`` on the create + list surfaces."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack


async def _create_echo_preset(stack: TaiStack, name: str, payload: str) -> dict:
    return await stack.api().post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": "echo probe preset",
            "fixed_kwargs": {"payload": payload},
        },
    )


async def test_create_reports_healthy_conflicted_shape(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    name = uniq("preset")
    created = await _create_echo_preset(agents_stack, name, uniq("payload"))
    assert created["conflicted"] is False
    assert created["conflicted_reason"] is None

    row = next(p for p in await agents_stack.api().get("/api/presets") if p["name"] == name)
    assert row["conflicted"] is False
    assert row["conflicted_reason"] is None


async def test_referees_lists_composing_presets(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = agents_stack.api()
    base = uniq("base")
    await _create_echo_preset(agents_stack, base, uniq("payload"))

    # No preset composes it yet.
    referees = await api.get(f"/api/presets/{base}/referees")
    assert referees == {"name": base, "referees": []}

    # An authored-agent preset that names ``base`` in its tool_names is a referee.
    composer = uniq("composer")
    await api.post(
        "/api/presets",
        json={
            "name": composer,
            "base_tool": "tools_agent",
            "description": "agent composing the base preset",
            "fixed_kwargs": {"tool_names": [base]},
        },
    )
    referees = await api.get(f"/api/presets/{base}/referees")
    assert referees == {"name": base, "referees": [composer]}

    # A rename of the referenced preset is blocked, naming the referee.
    blocked = await api.request_raw("POST", f"/api/presets/{base}/rename", json={"new_name": uniq("renamed")})
    assert blocked.status_code == 409, blocked.text
    assert composer in blocked.json()["error"]


async def test_referees_unknown_preset_404(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    resp = await agents_stack.api().request_raw("GET", f"/api/presets/{uniq('ghost')}/referees")
    assert resp.status_code == 404, resp.text


async def test_validate_create_mode(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = agents_stack.api()

    valid = await api.post(
        "/api/presets/validate",
        json={"name": uniq("draft"), "base_tool": "e2e_echo", "fixed_kwargs": {"payload": "ok"}},
    )
    assert valid == {"valid": True, "error": None}

    unknown_base = await api.post("/api/presets/validate", json={"name": uniq("draft"), "base_tool": "ghost_tool"})
    assert unknown_base["valid"] is False
    assert "ghost_tool" in unknown_base["error"]

    invalid_combo = await api.post(
        "/api/presets/validate",
        json={"name": uniq("draft"), "base_tool": "e2e_echo", "extensions": [["ghost_ext"]]},
    )
    assert invalid_combo["valid"] is False
    assert invalid_combo["error"]

    # An ``output_schema`` must be an object schema (both bind dispatch paths require
    # an object root). A non-object schema — here an integer — is rejected up front
    # by the validate door's output-schema guard, before any dry-run bind, so this
    # exercises that guard rather than a bind-time mismatch.
    non_object_schema = await api.post(
        "/api/presets/validate",
        json={"name": uniq("draft"), "base_tool": "e2e_echo", "output_schema": {"type": "integer"}},
    )
    assert non_object_schema["valid"] is False
    assert "output_schema must be an object schema" in non_object_schema["error"]


async def test_validate_version_mode(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = agents_stack.api()
    name = uniq("preset")
    await _create_echo_preset(agents_stack, name, uniq("payload"))

    # An existing preset name resolves the validate door to VERSION mode.
    valid = await api.post("/api/presets/validate", json={"name": name, "fixed_kwargs": {"payload": uniq("v2")}})
    assert valid == {"valid": True, "error": None}

    invalid = await api.post("/api/presets/validate", json={"name": name, "extensions": [["ghost_ext"]]})
    assert invalid["valid"] is False
    assert invalid["error"]


async def test_version_tags_door(agents_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = agents_stack.api()
    name = uniq("preset")
    await _create_echo_preset(agents_stack, name, uniq("payload"))
    # A second version so the tag edit targets a specific one.
    await api.post(f"/api/presets/{name}/versions", json={"fixed_kwargs": {"payload": uniq("v2")}})

    before = await api.get(f"/api/presets/{name}/versions/2")
    tags = ["prod", "release-2026"]
    result = await api.request_raw("PUT", f"/api/presets/{name}/versions/2/tags", json={"tags": tags})
    assert result.status_code == 200, result.text
    assert result.json()["data"] == {"name": name, "version": 2, "tags": tags}

    versions = await api.get(f"/api/presets/{name}/versions")
    v2 = next(v for v in versions if v["version"] == 2)
    assert v2["tags"] == tags

    # Tags are an annotation on an immutable body — the version body is unchanged.
    after = await api.get(f"/api/presets/{name}/versions/2")
    assert after["body"] == before["body"]

    # An unknown version is a clean 404.
    missing = await api.request_raw("PUT", f"/api/presets/{name}/versions/99/tags", json={"tags": ["x"]})
    assert missing.status_code == 404, missing.text
