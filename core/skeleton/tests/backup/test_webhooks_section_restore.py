"""The ``webhooks`` restore paths: envelope round-trip, tombstone durability,
expiry/collision handling, topic-verifier bindings, and the in-memory seam."""

from __future__ import annotations

import pytest
from tai42_contract.hooks import HookParams

from tai42_skeleton.backup import webhooks_section
from tai42_skeleton.backup.registry import import_mode
from tai42_skeleton.hooks import trigger_links
from tai42_skeleton.hooks.trigger_links import TriggerLinkError, create_trigger_link, resolve_trigger_token

from .conftest import _link_record, _wipe

# -- envelope round-trip ------------------------------------------------------


async def test_envelope_roundtrip_timed_and_permanent(store) -> None:
    await store.manager.register(
        HookParams(name="h1", topic="t", tool="notify", execution_key="k-fire", execution_key_fingerprint="fp")
    )
    timed = await create_trigger_link(
        topic="t",
        name="timed",
        ttl_seconds=3600,
        tool_kwargs={"k": 1},
        execution_key="k-fire",
        execution_key_fingerprint="fp",
        require_api_key=False,
        created_by="a",
    )
    perm = await create_trigger_link(
        topic="t",
        name="perm",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp",
        require_api_key=False,
        created_by="a",
    )

    doc = await webhooks_section._export_webhooks()
    assert [h["name"] for h in doc["hooks"]] == ["h1"]
    assert {link["name"] for link in doc["trigger_links"]} == {"timed", "perm"}
    assert doc["tombstones"] == []

    _wipe(store)
    report = await webhooks_section._import_webhooks(doc)
    assert report["errors"] == []
    assert report["created"] == 3  # one hook + two links
    # Both original URLs resolve again, still bound to the key their records named.
    assert (await resolve_trigger_token(timed["token"])).topic == "t"
    restored = await resolve_trigger_token(perm["token"])
    assert (restored.topic, restored.execution_key) == ("t", "k-fire")


async def test_tool_kwargs_survive_roundtrip_and_merge(store) -> None:
    link = await create_trigger_link(
        topic="t",
        name="k",
        ttl_seconds=None,
        tool_kwargs={"flow": {"x": 1}},
        execution_key="k-fire",
        execution_key_fingerprint="fp",
        require_api_key=False,
        created_by=None,
    )
    doc = await webhooks_section._export_webhooks()
    _wipe(store)
    await webhooks_section._import_webhooks(doc)
    assert (await resolve_trigger_token(link["token"])).tool_kwargs == {"flow": {"x": 1}}


# -- tombstone durability -----------------------------------------------------


async def test_import_pre_revocation_export_stays_dead_via_local_tombstone(store) -> None:
    link = await create_trigger_link(
        topic="t",
        name="r",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp",
        require_api_key=False,
        created_by=None,
    )
    pre = await webhooks_section._export_webhooks()  # exported while live (no tombstone yet)
    await trigger_links.revoke_trigger_link("r")  # local tombstone now guards it
    report = await webhooks_section._import_webhooks(pre)
    assert report["skipped"] >= 1  # the tombstoned record is refused
    with pytest.raises(TriggerLinkError):
        await resolve_trigger_token(link["token"])


async def test_exported_tombstone_restores_and_gates(store) -> None:
    link = await create_trigger_link(
        topic="t",
        name="r",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp",
        require_api_key=False,
        created_by=None,
    )
    pre = await webhooks_section._export_webhooks()  # pre-revocation (record live, no tombstone)
    await trigger_links.revoke_trigger_link("r")
    post = await webhooks_section._export_webhooks()  # post-revocation (tombstone present)
    assert len(post["tombstones"]) == 1

    _wipe(store)
    await webhooks_section._import_webhooks(post)  # restores the tombstone, no live record
    with pytest.raises(TriggerLinkError):
        await resolve_trigger_token(link["token"])
    # A follow-up import of the PRE-revocation export stays dead too (tombstone gates).
    report = await webhooks_section._import_webhooks(pre)
    assert report["skipped"] >= 1
    with pytest.raises(TriggerLinkError):
        await resolve_trigger_token(link["token"])


# -- expiry / collisions ------------------------------------------------------


async def test_expired_at_import_skipped_and_logged(store, caplog) -> None:
    doc = {
        "hooks": [],
        "trigger_links": [
            {
                "name": "exp",
                "token_hash": "a" * 64,
                "record": {
                    "name": "exp",
                    "topic": "t",
                    "execution_key": "k-fire",
                    "execution_key_fingerprint": "fp",
                    "require_api_key": False,
                    "tool_kwargs": None,
                    "created_by": None,
                    "created_at": "2000-01-01T00:00:00+00:00",
                    "expires_at": "2000-01-02T00:00:00+00:00",
                },
            }
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)
    assert report["skipped"] == 1
    assert report["created"] == 0


async def test_live_name_collision_counts_updated(store) -> None:
    await create_trigger_link(
        topic="t",
        name="shared",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp",
        require_api_key=False,
        created_by=None,
    )
    doc = {
        "hooks": [],
        "trigger_links": [
            {
                "name": "shared",
                "token_hash": "b" * 64,
                "record": {
                    "name": "shared",
                    "topic": "t",
                    "execution_key": "k-fire",
                    "execution_key_fingerprint": "fp",
                    "require_api_key": False,
                    "tool_kwargs": None,
                    "created_by": None,
                    "created_at": "2026-07-21T00:00:00+00:00",
                    "expires_at": None,
                },
            }
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    # Under overwrite the live link (keyed by name) is re-keyed: an update.
    with import_mode("overwrite"):
        report = await webhooks_section._import_webhooks(doc)
    assert report["updated"] == 1

    # Under skip (the default) the live link is left untouched — a clean skip, and its
    # original hash stands rather than being re-keyed to the imported one.
    skip = await webhooks_section._import_webhooks(doc)
    assert skip["updated"] == 0
    assert skip["skipped_existing"] == 1


async def test_one_hash_two_names_whole_section_refusal_zero_written(store) -> None:
    await store.manager.register(
        HookParams(name="h", topic="t", tool="notify", execution_key="k-fire", execution_key_fingerprint="fp")
    )
    rec = {
        "name": "",
        "topic": "t",
        "execution_key": "k-fire",
        "execution_key_fingerprint": "fp",
        "require_api_key": False,
        "tool_kwargs": None,
        "created_by": None,
        "created_at": "2026-07-21T00:00:00+00:00",
        "expires_at": None,
    }
    doc = {
        "hooks": [
            {
                "name": "new-hook",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            }
        ],
        "trigger_links": [
            {"name": "A", "token_hash": "c" * 64, "record": {**rec, "name": "A"}},
            {"name": "B", "token_hash": "c" * 64, "record": {**rec, "name": "B"}},
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    with pytest.raises(ValueError, match="two names"):  # whole-section refusal (zero keys written)
        await webhooks_section._import_webhooks(doc)
    # Zero keys written — even the hooks portion. The pre-existing hook "h" is
    # untouched; "new-hook" was never registered.
    hooks = await store.manager.list_hooks()
    assert set(hooks) == {"h"}


async def test_same_name_twice_last_wins(store) -> None:
    rec = {
        "topic": "t",
        "execution_key": "k-fire",
        "execution_key_fingerprint": "fp",
        "require_api_key": False,
        "tool_kwargs": None,
        "created_by": None,
        "created_at": "2026-07-21T00:00:00+00:00",
        "expires_at": None,
    }
    h1, h2 = "d" * 64, "e" * 64
    doc = {
        "hooks": [],
        "trigger_links": [
            {"name": "A", "token_hash": h1, "record": {**rec, "name": "A"}},
            {"name": "A", "token_hash": h2, "record": {**rec, "name": "A"}},
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)
    assert report["errors"] == []
    # H2 is the live record; H1's record was displaced (deleted, NO tombstone).
    assert store.settings.trigger_record_key(h2) in store.redis._strings
    assert store.settings.trigger_record_key(h1) not in store.redis._strings
    assert trigger_links._as_str(store.redis._get_str(store.settings.trigger_name_key("A"))) == h2
    assert store.redis._get_str(store.settings.trigger_tomb_key(h1)) is None


async def test_live_index_dup_hash_whole_section_refusal(store) -> None:
    # Live A→H in the store; the payload carries B→H → whole-section refusal.
    link = await create_trigger_link(
        topic="t",
        name="A",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp",
        require_api_key=False,
        created_by=None,
    )
    live = (await trigger_links.export_trigger_links())["trigger_links"][0]
    doc = {
        "hooks": [],
        "trigger_links": [{"name": "B", "token_hash": live["token_hash"], "record": {**live["record"], "name": "B"}}],
        "topic_verifiers": {},
        "tombstones": [],
    }
    with pytest.raises(ValueError, match="already live under"):
        await webhooks_section._import_webhooks(doc)
    # A still resolves — nothing applied.
    assert (await resolve_trigger_token(link["token"])).topic == "t"


async def test_orphan_index_dup_hash_whole_section_refusal_zero_written(store) -> None:
    # An ORPHAN in the store: name key A → hash H with NO record key (a corrupt
    # hand-edited backup). A normal create can't produce this, so seed raw state.
    # Import binds a NEW name B to that same H → whole-section refusal, zero written:
    # writing B→H would let a later revoke of A destroy B's live record.
    orphan_hash = "c" * 64
    store.redis._set_str(store.settings.trigger_name_key("A"), orphan_hash)
    await store.manager.register(
        HookParams(name="h", topic="t", tool="notify", execution_key="k-fire", execution_key_fingerprint="fp")
    )
    doc = {
        "hooks": [
            {
                "name": "new-hook",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            }
        ],
        "trigger_links": [
            {
                "name": "B",
                "token_hash": orphan_hash,
                "record": {
                    "name": "B",
                    "topic": "t",
                    "execution_key": "k-fire",
                    "execution_key_fingerprint": "fp",
                    "require_api_key": False,
                    "tool_kwargs": None,
                    "created_by": None,
                    "created_at": "2026-07-21T00:00:00+00:00",
                    "expires_at": None,
                },
            }
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    with pytest.raises(ValueError, match="already live under"):
        await webhooks_section._import_webhooks(doc)
    # Zero keys written: B never bound, its record never written, hooks untouched.
    assert store.redis._get_str(store.settings.trigger_name_key("B")) is None
    assert store.settings.trigger_record_key(orphan_hash) not in store.redis._strings
    assert set(await store.manager.list_hooks()) == {"h"}


# -- topic verifier bindings (the topic's ingress lock) -----------------------


async def test_verifier_binding_round_trips_so_a_verified_topic_stays_verified(store) -> None:
    # A binding is the topic's ingress lock: dropping it on restore would bring the
    # topic's hooks back on a door anyone may ring unsigned.
    await store.manager.set_topic_verifier(
        "notifications", {"verifier": "github", "config": {"secret_env": "GH_SECRET"}}
    )
    await store.manager.register(
        HookParams(
            name="n1", topic="notifications", tool="notify", execution_key="k-fire", execution_key_fingerprint="fp"
        )
    )

    doc = await webhooks_section._export_webhooks()
    assert doc["topic_verifiers"] == {"notifications": {"verifier": "github", "config": {"secret_env": "GH_SECRET"}}}

    _wipe(store)
    report = await webhooks_section._import_webhooks(doc)
    assert report["errors"] == []
    assert report["created"] == 2  # the binding + the hook
    assert await store.manager.get_topic_verifier("notifications") == {
        "verifier": "github",
        "config": {"secret_env": "GH_SECRET"},
    }


async def test_replacing_a_live_binding_counts_updated(store) -> None:
    await store.manager.set_topic_verifier("notifications", {"verifier": "github", "config": {}})
    doc = {
        "hooks": [],
        "topic_verifiers": {"notifications": {"verifier": "hmac", "config": {}}},
        "trigger_links": [],
        "tombstones": [],
    }
    # Overwrite replaces the live binding (keyed by topic): an update.
    with import_mode("overwrite"):
        report = await webhooks_section._import_webhooks(doc)
    assert report["errors"] == []
    assert (report["updated"], report["created"]) == (1, 0)
    assert (await store.manager.get_topic_verifier("notifications"))["verifier"] == "hmac"


async def test_skip_leaves_a_live_binding_untouched(store) -> None:
    await store.manager.set_topic_verifier("notifications", {"verifier": "github", "config": {}})
    doc = {
        "hooks": [],
        "topic_verifiers": {"notifications": {"verifier": "hmac", "config": {}}},
        "trigger_links": [],
        "tombstones": [],
    }
    # Skip (the default) leaves the existing verifier in place — the topic's lock is not
    # swapped and the record is counted as a clean skip.
    report = await webhooks_section._import_webhooks(doc)
    assert report["errors"] == []
    assert (report["updated"], report["created"], report["skipped_existing"]) == (0, 0, 1)
    assert (await store.manager.get_topic_verifier("notifications"))["verifier"] == "github"


@pytest.mark.parametrize("binding", [{"config": {}}, {"verifier": "", "config": {}}, "hmac", {"verifier": 1}])
async def test_malformed_binding_per_topic_error_rest_restored(store, binding) -> None:
    # A hand-edited binding is a loud per-topic rejection in the report — never a
    # malformed binding stored, and never an abort of the records around it.
    doc = {
        "hooks": [
            {"name": "h", "topic": "t", "tool": "notify", "execution_key": "k-fire", "execution_key_fingerprint": "fp"}
        ],
        "topic_verifiers": {"broken": binding, "sound": {"verifier": "hmac", "config": {}}},
        "trigger_links": [],
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)
    assert len(report["errors"]) == 1
    assert "broken" in report["errors"][0]
    assert report["skipped"] == 1
    assert report["created"] == 2  # the sound binding + the hook
    assert await store.manager.get_topic_verifier("broken") is None
    assert set(await store.manager.list_hooks()) == {"h"}


async def test_an_offset_less_expires_at_is_a_per_record_error_not_a_torn_section(store) -> None:
    # An ``expires_at`` that parses but carries no UTC offset is a record-shaped fault:
    # ``errors`` + ``skipped``, links either side still restore, no mid-write abort.
    doc = {
        "hooks": [],
        "topic_verifiers": {},
        "trigger_links": [
            {"name": "good", "token_hash": "a" * 64, "record": _link_record("good", "k-fire")},
            {
                "name": "naive",
                "token_hash": "b" * 64,
                "record": {**_link_record("naive", "k-fire"), "expires_at": "2030-01-01T00:00:00"},
            },
            {"name": "after", "token_hash": "c" * 64, "record": _link_record("after", "k-fire")},
        ],
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert len(report["errors"]) == 1
    assert "carries no timezone offset" in report["errors"][0]
    assert (report["created"], report["skipped"]) == (2, 1)
    assert store.settings.trigger_record_key("b" * 64) not in store.redis._strings
    assert store.settings.trigger_record_key("c" * 64) in store.redis._strings


async def test_records_on_a_topic_whose_lock_failed_are_refused(store) -> None:
    # Ingress locks restore BEFORE the records they gate, and a lock that failed to land
    # leaves its topic's door open: every record on that topic is refused, others stand.
    doc = {
        "hooks": [
            {
                "name": "n1",
                "topic": "notifications",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            },
            {
                "name": "other",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            },
        ],
        "topic_verifiers": {"notifications": {"verifier": "", "config": {}}},
        "trigger_links": [
            {
                "name": "eventlink",
                "token_hash": "a" * 64,
                "record": _link_record("eventlink", "k-fire", topic="notifications"),
            },
            {"name": "otherlink", "token_hash": "b" * 64, "record": _link_record("otherlink", "k-fire")},
        ],
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert await store.manager.all_topic_verifiers() == {}
    assert set(await store.manager.list_hooks()) == {"other"}
    assert store.settings.trigger_record_key("a" * 64) not in store.redis._strings
    assert store.settings.trigger_record_key("b" * 64) in store.redis._strings
    # The binding's own failure, plus one per record it could not gate.
    assert len(report["errors"]) == 3
    assert report["skipped"] == 3
    assert report["created"] == 2


@pytest.mark.parametrize("topic", ["", 123])
async def test_ill_typed_topic_key_per_item_error_section_proceeds(store, topic) -> None:
    # A blank or ill-typed topic key names no door; it is a per-item report error,
    # never a binding written under a topic nothing can deliver to.
    doc = {
        "hooks": [],
        "topic_verifiers": {topic: {"verifier": "hmac", "config": {}}},
        "trigger_links": [],
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)
    assert len(report["errors"]) == 1
    assert report["skipped"] == 1
    assert report["created"] == 0


async def test_binding_naming_an_unregistered_verifier_is_restored_not_dropped(store) -> None:
    # An unknown verifier NAME still restores: the ingress door resolves it live and
    # denies what it cannot resolve, whereas refusing here would restore a PUBLIC topic.
    doc = {
        "hooks": [],
        "topic_verifiers": {"notifications": {"verifier": "not-installed-here", "config": {}}},
        "trigger_links": [],
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)
    assert report["errors"] == []
    assert report["created"] == 1
    assert (await store.manager.get_topic_verifier("notifications"))["verifier"] == "not-installed-here"


# -- restore into a now-verified topic (fire-time enforcement) -------------


async def test_restore_into_verified_topic_created_but_resolves_404(store) -> None:
    link = await create_trigger_link(
        topic="secure",
        name="v",
        ttl_seconds=None,
        tool_kwargs=None,
        execution_key="k-fire",
        execution_key_fingerprint="fp",
        require_api_key=False,
        created_by=None,
    )
    doc = await webhooks_section._export_webhooks()
    _wipe(store)
    # The topic gains a verifier binding after the export.
    await store.manager.set_topic_verifier("secure", {"verifier": "hmac", "config": {}})
    report = await webhooks_section._import_webhooks(doc)
    assert report["created"] == 1  # restore does NOT re-run the create-time verifier check
    with pytest.raises(TriggerLinkError):  # but the door enforces it (uniform 404)
        await resolve_trigger_token(link["token"])


# -- in-memory seam -----------------------------------------------------------


async def test_in_memory_export_truthfully_empty_hooks_unchanged(in_memory_store) -> None:
    await in_memory_store.manager.register(
        HookParams(name="h1", topic="t", tool="notify", execution_key="k-fire", execution_key_fingerprint="fp")
    )
    doc = await webhooks_section._export_webhooks()
    assert [h["name"] for h in doc["hooks"]] == ["h1"]
    assert doc["trigger_links"] == []
    assert doc["tombstones"] == []


async def test_in_memory_import_refuses_trigger_portion_hooks_restore(in_memory_store, caplog) -> None:
    doc = {
        "hooks": [
            {"name": "h1", "topic": "t", "tool": "notify", "execution_key": "k-fire", "execution_key_fingerprint": "fp"}
        ],
        "trigger_links": [
            {
                "name": "x",
                "token_hash": "a" * 64,
                "record": {
                    "name": "x",
                    "topic": "t",
                    "execution_key": "k-fire",
                    "execution_key_fingerprint": "fp",
                    "require_api_key": False,
                    "tool_kwargs": None,
                    "created_by": None,
                    "created_at": "2026-07-21T00:00:00+00:00",
                    "expires_at": None,
                },
            }
        ],
        "topic_verifiers": {},
        "tombstones": ["b" * 64],
    }
    report = await webhooks_section._import_webhooks(doc)
    # The hooks portion restores; the trigger + tombstone portions refuse loudly.
    assert set(await in_memory_store.manager.list_hooks()) == {"h1"}
    assert len(report["errors"]) == 2  # one for the record, one for the tombstone
    assert report["created"] == 1
