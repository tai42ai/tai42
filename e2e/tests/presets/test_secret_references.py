"""A preset secret reference round-trips every store/read door as the marker, resolves
to the referenced value at dispatch, and is masked wherever a run is recorded.

A ``fixed_kwargs`` scalar leaf written ``!ENV ${VAR}`` is a secret reference: the store
keeps the marker verbatim and ``preset_bind`` resolves it against the server's
environment at bind. On a stack whose skeleton process carries the referenced variable
this suite drives, over the REAL versioning store, one preset over the
``e2e_secret_ref_sink`` probe (which returns what it received):

* every read door — HTTP get / version / backup export, the MCP-projected read — returns
  the marker, never the resolved value; the record-only list view carries no kwargs body;
* the live sync run-tool door reveals the resolved value to the caller (the probe
  received it), while a background-recorded run stores only the masked placeholder;
* a new version + rollback keep the marker;
* creating a preset that references an UNSET variable is refused with a 400 naming it.

A second preset over ``e2e_secret_ref_typed_sink`` (``token: str``) proves the common
real case: the reference is revealed to its plain string so it reaches a TYPED-scalar
parameter, which a ``SecretValue`` wrapper would fail at argument validation.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from tai42_contract.secrets import SECRET_PLACEHOLDER

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import build_bare_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import StackConfig, StackResources

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

_SECRET_ENV = "E2E_PRESET_SECRET_REF"
_SECRET_VALUE = "resolved-stub-credential"
_MARKER = f"!ENV ${{{_SECRET_ENV}}}"
_BASE_TOOL = "e2e_secret_ref_sink"
_TYPED_BASE_TOOL = "e2e_secret_ref_typed_sink"


def _build_secret_ref_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The bare core surface plus the backup router, the secret-reference probe tool, and
    the referenced variable set in the skeleton's environment."""
    cfg = build_bare_stack(res, variants)
    manifest = copy.deepcopy(cfg.manifest)
    manifest["routers_modules"] = [*manifest["routers_modules"], "tai42_skeleton.routers.backup"]
    manifest["tools"] = [
        *manifest["tools"],
        {
            "title": "secret-ref-probe",
            "module": "tai42_e2e_fixtures.secret_ref_probe",
            "include": [_BASE_TOOL, _TYPED_BASE_TOOL],
        },
    ]
    env = {**cfg.env, _SECRET_ENV: _SECRET_VALUE}
    return replace(cfg, name="preset-secret-ref", manifest=manifest, env=env, run_metrics=False)


async def _create(stack: TaiStack, name: str, marker: str) -> dict:
    return await stack.api().post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": _BASE_TOOL,
            "description": "secret-reference probe preset",
            "fixed_kwargs": {"payload": marker},
        },
    )


async def test_secret_reference_round_trips_every_door(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(_build_secret_ref_stack)
    api = stack.api()
    name = uniq("refpreset")
    await _create(stack, name, _MARKER)

    # -- reads: every store door returns the marker, never the resolved value ----------
    detail = await api.get(f"/api/presets/{name}")
    assert detail["fixed_kwargs"] == {"payload": _MARKER}

    version = await api.get(f"/api/presets/{name}/versions/1")
    assert version["body"]["fixed_kwargs"] == {"payload": _MARKER}

    # The list view is record metadata only — it carries no kwargs body, so neither the
    # marker nor a resolved value can ride it.
    row = next(p for p in await api.get("/api/presets") if p["name"] == name)
    assert "fixed_kwargs" not in row
    assert _SECRET_VALUE not in json.dumps(row)

    # The backup export carries the versioned body verbatim — the marker survives.
    export = await api.request_raw("POST", "/api/backup/export", json={"sections": ["versioned_documents"]})
    assert export.status_code == 200, export.text
    document = export.json()
    exported = document["sections"]["versioned_documents"]
    ref_version = next(v for v in exported["versions"] if isinstance(v["body"], dict) and v["body"].get("fixed_kwargs"))
    assert ref_version["body"]["fixed_kwargs"] == {"payload": _MARKER}
    assert _SECRET_VALUE not in json.dumps(document)

    # Re-importing the exported document keeps the marker in the store body.
    imported = await api.post(
        "/api/backup/import",
        json={"document": document, "sections": ["versioned_documents"]},
        retry_on_reloading=True,
    )
    assert imported["ok"], imported
    reread = await api.get(f"/api/presets/{name}")
    assert reread["fixed_kwargs"] == {"payload": _MARKER}

    # The MCP-projected read serves the SAME operation the HTTP door does — marker verbatim.
    async with stack.mcp(port=stack.port_a) as mcp:
        projected = (await mcp.call_tool("get_preset", {"name": name}, retry_on_reloading=True)).data
    assert projected["fixed_kwargs"] == {"payload": _MARKER}

    # -- dispatch: the probe receives the resolved value; the record masks it ----------
    revealed = await api.post("/api/run-tool", json={"tool_name": name, "arguments": {}})
    assert revealed == {"payload": _SECRET_VALUE}, revealed

    submitted = await api.post("/api/tool-runs", json={"tool_name": name, "arguments": {}}, expect=202)
    run_id = submitted["run_id"]

    async def terminal() -> dict | None:
        record = await api.get(f"/api/tool-runs/{run_id}")
        return record if record["status"] == "succeeded" else None

    record = await wait_for_async(terminal, deadline=10.0, message="background secret-reference run never succeeded")
    assert record["result"] == {"payload": SECRET_PLACEHOLDER}, record
    assert _SECRET_VALUE not in json.dumps(record), record

    # -- a new version + rollback keep the marker -------------------------------------
    await api.post(f"/api/presets/{name}/versions", json={"fixed_kwargs": {"payload": _MARKER}})
    v2 = await api.get(f"/api/presets/{name}/versions/2")
    assert v2["body"]["fixed_kwargs"] == {"payload": _MARKER}

    await api.post(f"/api/presets/{name}/rollback", json={"version": 1})
    after_rollback = await api.get(f"/api/presets/{name}")
    assert after_rollback["fixed_kwargs"] == {"payload": _MARKER}

    # -- negative: a reference to an UNSET variable is refused at create with a 400 -----
    unset_var = "E2E_PRESET_SECRET_ABSENT"
    resp = await api.request_raw(
        "POST",
        "/api/presets",
        json={
            "name": uniq("badref"),
            "base_tool": _BASE_TOOL,
            "description": "references an unset variable",
            "fixed_kwargs": {"payload": f"!ENV ${{{unset_var}}}"},
        },
    )
    assert resp.status_code == 400, resp.text
    assert unset_var in resp.text


async def test_secret_reference_reaches_a_typed_str_parameter(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """The common real case: a reference baked into a ``token: str`` parameter resolves at
    dispatch and reaches the typed parameter as the plain string.

    A ``SecretValue`` is not a ``str``, so the resolved reference is revealed to its plain
    value before the base tool's pydantic argument validation. The read door still returns
    the marker verbatim; the sync run-tool door proves the resolved string arrived at the
    typed parameter (by its length and last character) without the wire carrying it.
    """
    stack = fresh_stack(_build_secret_ref_stack)
    api = stack.api()
    name = uniq("typedref")
    await api.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": _TYPED_BASE_TOOL,
            "description": "typed secret-reference probe preset",
            "fixed_kwargs": {"token": _MARKER},
        },
    )

    detail = await api.get(f"/api/presets/{name}")
    assert detail["fixed_kwargs"] == {"token": _MARKER}

    received = await api.post("/api/run-tool", json={"tool_name": name, "arguments": {}})
    assert received == {"type": "str", "length": len(_SECRET_VALUE), "last": _SECRET_VALUE[-1]}, received
    assert _SECRET_VALUE not in json.dumps(received)
