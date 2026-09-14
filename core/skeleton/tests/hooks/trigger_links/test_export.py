"""Export and the bound-hashes index: hashes and tombstones carried, no raw token,
multipage no-token scan, and orphans surfaced by the bound-hashes index that the
orphan-skipping export omits."""

from __future__ import annotations

import json

from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.hooks.trigger_links import (
    bound_hashes_by_name,
    create_trigger_link,
    export_trigger_links,
    revoke_trigger_link,
)


async def test_export_carries_hashes_and_tombstones_no_token_multipage(store) -> None:
    tokens = []
    for i in range(15):
        r = await create_trigger_link(
            topic="t",
            name=f"e{i:02d}",
            ttl_seconds=None,
            tool_kwargs=None,
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            require_api_key=False,
            created_by=None,
        )
        tokens.append(r["token"])
    await revoke_trigger_link("e00")  # one tombstone
    exported = await export_trigger_links()
    assert len(exported["trigger_links"]) == 14  # the revoked one is gone from the index
    assert len(exported["tombstones"]) == 1
    dumped = json.dumps(exported)
    for token in tokens:
        assert token not in dumped
    for item in exported["trigger_links"]:
        assert len(item["token_hash"]) == 64


async def test_bound_hashes_by_name_includes_orphans(store) -> None:
    # A live link plus a PERMANENT orphan (name key → hash with no record). The
    # bound-hashes index the import conflict check reads MUST surface both, unlike the
    # orphan-skipping export.
    live = await create_trigger_link(
        topic="t",
        name="alive",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        require_api_key=False,
        created_by=None,
    )
    live_hash = hash_api_key(live["token"])
    orphan_hash = "a" * 64
    store.redis._set_str(store.settings.trigger_name_key("orphan"), orphan_hash)

    bindings = await bound_hashes_by_name()
    assert bindings == {"alive": live_hash, "orphan": orphan_hash}
    # The orphan-skipping export omits the orphan — the exact gap this index closes.
    exported = await export_trigger_links()
    assert {e["name"] for e in exported["trigger_links"]} == {"alive"}
