"""Fleet-wide template-cache eviction over the worker bus.

The compiled template is held in a per-worker cache, so a store write on one worker
leaves every sibling rendering the old body. Each write/delete/dir-delete op writes the
store, then broadcasts an ``evict_template`` eviction over the worker bus, so every
worker drops the stale compilation; the op response embeds the per-worker ``fanout``
report.

Driven over the two-replica ``replicas_stack`` (two ``--workers 1`` masters on ports A
and B, sharing config/Redis/PG and the worker bus, over the real active storage
provider): a template is COMPILED into replica B's cache by a render there, then mutated
through replica A, and a subsequent render on B must reflect the mutation — proving B
dropped its stale compilation on the fanout, not merely that the store changed.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:storage", "topology:replicas")


def _assert_fleet_fanout(response: dict) -> None:
    """The op response embeds a reachable multi-worker fan-out report — the two-replica
    fleet, every worker confirmed."""
    fanout = response.get("fanout")
    assert isinstance(fanout, dict), f"the template op carried no fanout report: {response}"
    assert fanout["mode"] == "fleet", f"the template op did not fan out over the fleet: {fanout}"
    assert fanout["results"], f"the fanout reported no per-worker outcomes: {fanout}"


async def _upload(api, path: str, content: str) -> dict:
    return await api.post("/api/upload-template", json={"path": path, "content": content}, retry_on_reloading=True)


async def _render(api, template_id: str, *, expect: int = 200) -> dict:
    return await api.post(
        "/api/render-template", json={"text": {"id": template_id}}, expect=expect, retry_on_reloading=True
    )


async def test_template_upload_evicts_stale_compilation_fleet_wide(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """An overwrite through replica A evicts the stale compiled template on replica B:
    B first compiles v1 into its cache, then A overwrites to v2 (its op response carries
    the per-worker fanout), and a render on B returns v2 — the eviction dropped B's stale
    compilation. Without the fanout B would keep serving the compiled v1."""
    path = f"{uniq('evict')}.txt"
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    # Write v1 through A (fans the initial eviction out) and compile it into B's cache.
    _assert_fleet_fanout(await _upload(api_a, path, "v1-body"))
    assert (await _render(api_b, path))["rendered"] == "v1-body"

    # Overwrite to v2 through A: the response carries the fleet fanout, and B's stale
    # compilation is evicted on the broadcast.
    _assert_fleet_fanout(await _upload(api_a, path, "v2-body"))

    # Replica B now renders the NEW body — it dropped the compiled v1 and re-read the store.
    reread_b = await _render(api_b, path)
    assert reread_b["rendered"] == "v2-body", f"replica B served a stale compilation after the eviction: {reread_b}"


async def test_template_dir_delete_evicts_prefix_fleet_wide(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A directory delete through replica A evicts every stale compilation under the
    prefix on replica B: B compiles a template under the directory, A deletes the whole
    directory (its op response carries the per-worker fanout, prefix semantics), and a
    render of the now-gone template on B is a loud 404 — B's cached compilation was
    evicted, so it re-reads the store and finds nothing rather than serving the stale body."""
    directory = uniq("dir")
    path = f"{directory}/leaf.txt"
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    # Write a template under the directory through A and compile it into B's cache.
    _assert_fleet_fanout(await _upload(api_a, path, "leaf-body"))
    assert (await _render(api_b, path))["rendered"] == "leaf-body"

    # Delete the whole directory through A: the response carries the fleet fanout, and the
    # prefix eviction drops B's stale compilation under the directory.
    deleted = await api_a.post("/api/delete-template-dir", json={"path": directory}, retry_on_reloading=True)
    _assert_fleet_fanout(deleted)

    # Replica B no longer finds the deleted template — it re-read the store past the
    # evicted compilation and gets a loud 404, not the stale body.
    missing = await _render(api_b, path, expect=404)
    assert path in missing["error"], missing


@pytest.mark.needs("kind:states")
async def test_a_resource_upload_rerenders_the_template_jq_that_reads_it_on_every_worker(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A template program stored by id is rendered once per worker and served from its cache; an
    upload of the resource through replica A re-renders it on A (the origin's local eviction) and
    on B (the fleet eviction), so both evaluate the NEW program text."""
    path = f"{uniq('program')}.jq"
    state = uniq("status")
    template = uniq("by-id-tmpl").replace("_", "-")
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    _assert_fleet_fanout(await _upload(api_a, path, '"first program"'))
    await api_a.put(
        f"/api/states/{state}",
        json={
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )
    await api_a.put(
        f"/api/state-templates/{template}",
        json={
            "kind": "state-template",
            "name": template,
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "template_jq": {"probe": {"purpose": "input", "jq": {"id": path}}},
        },
    )
    await api_a.put(f"/api/states/{state}/attachments/{template}", json={"path": ["box"]})
    record = f"/api/states/{state}/records/agent/a-42/thread/t1/template-jq/probe"
    for api in (api_a, api_b):
        assert (await api.get(record, retry_on_reloading=True))["value"] == "first program"

    _assert_fleet_fanout(await _upload(api_a, path, '"second program"'))
    for api in (api_a, api_b):
        evaluated = await api.get(record, retry_on_reloading=True)
        assert evaluated["value"] == "second program", f"a worker served a stale rendered program: {evaluated}"
