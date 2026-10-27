"""The conversation keyspace: each key under its own segment of the prefix, and the refusal of a blank or
boundary-moving segment."""

from __future__ import annotations

from tai42_skeleton.conversations.settings import ConversationsSettings


def test_keyspace_helpers_are_distinct_greppable_segments():
    s = ConversationsSettings()
    # Each keyspace its own segment under the shared prefix; the provider / outbound id
    # and the ``:``-bearing thread id sit LAST so nothing bleeds across a segment.
    assert s.keyspace.dedupe_key("twilio", "SM:1") == "conversations:dedupe:twilio:SM:1"
    assert s.keyspace.record_key("m-1") == "conversations:record:m-1"
    assert s.keyspace.outbound_index_key("twilio", "OB:2") == "conversations:outbound:twilio:OB:2"
    assert s.keyspace.route_key("chat-line") == "conversations:route:chat-line"
    assert s.keyspace.route_names_key == "conversations:route_names"
    assert s.keyspace.route_key_prefix == "conversations:route:"
    assert s.keyspace.thread_index_key("chat-line", "bridge:chat-line:+1555") == (
        "conversations:thread:chat-line:bridge:chat-line:+1555"
    )
    assert s.keyspace.route_threads_key("chat-line") == "conversations:route_threads:chat-line"
    # Person-linking keyspaces: the uuid4/hex ids sit LAST; the charset-unconstrained
    # target_name sits TERMINAL under a ``:``-free target_kind.
    assert s.keyspace.person_key("p-1") == "conversations:person:p-1"
    assert s.keyspace.person_key_prefix == "conversations:person:"
    assert s.keyspace.person_index_key("agent", "my:agent") == "conversations:person_index:agent:my:agent"
    assert s.keyspace.person_index_key_prefix == "conversations:person_index:"
    assert s.keyspace.pair_code_key("abc123") == "conversations:pair_code:abc123"
    assert s.keyspace.pair_code_key_prefix == "conversations:pair_code:"
    # open_code_key folds its inputs into ONE opaque sha256 terminal segment (deterministic).
    open_key = s.keyspace.open_code_key("agent", "my:agent", '["twilio","+1","+2"]')
    assert open_key.startswith("conversations:open_code:")
    assert len(open_key.removeprefix("conversations:open_code:")) == 64
    assert s.keyspace.open_code_key("agent", "my:agent", '["twilio","+1","+2"]') == open_key


def test_new_keyspace_blank_segments_are_refused():
    import pytest

    s = ConversationsSettings()
    with pytest.raises(ValueError, match="person_id must be a non-blank string"):
        s.keyspace.person_key("  ")
    with pytest.raises(ValueError, match="target_name must be a non-blank string"):
        s.keyspace.person_index_key("agent", "")
    with pytest.raises(ValueError, match="target_kind must not contain ':'"):
        s.keyspace.person_index_key("ag:ent", "assistant")
    with pytest.raises(ValueError, match="code_hash must be a non-blank string"):
        s.keyspace.pair_code_key("")


def test_a_blank_provider_supplied_segment_is_refused():
    import pytest

    s = ConversationsSettings()
    # Every blank id builds the SAME key, so two messages would share one idempotency
    # marker; refused loudly instead.
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="provider_message_id must be a non-blank string"):
            s.keyspace.dedupe_key("twilio", blank)
        with pytest.raises(ValueError, match="channel must be a non-blank string"):
            s.keyspace.dedupe_key(blank, "SM1")
        with pytest.raises(ValueError, match="outbound_message_id must be a non-blank string"):
            s.keyspace.outbound_index_key("twilio", blank)
        with pytest.raises(ValueError, match="channel must be a non-blank string"):
            s.keyspace.outbound_index_key(blank, "OB1")
        with pytest.raises(ValueError, match="thread_id must be a non-blank string"):
            s.keyspace.thread_index_key("chat-line", blank)
        with pytest.raises(ValueError, match="route_name must be a non-blank string"):
            s.keyspace.route_threads_key(blank)


def test_a_colon_in_the_channel_segment_is_refused():
    import pytest

    s = ConversationsSettings()
    # A ``:`` in the channel would move the boundary — ('a', 'b:c') and ('a:b', 'c')
    # would collide. The trailing provider id is unconstrained (nothing follows it).
    for builder in (s.keyspace.dedupe_key, s.keyspace.outbound_index_key):
        with pytest.raises(ValueError, match="channel must not contain ':'"):
            builder("a:b", "c")
    # The route name precedes the thread id, which carries ``:`` of its own.
    with pytest.raises(ValueError, match="route_name must not contain ':'"):
        s.keyspace.thread_index_key("a:b", "c")


def test_a_blank_overlap_cancel_thread_id_is_refused():
    import pytest

    s = ConversationsSettings()
    with pytest.raises(ValueError, match="thread_id must be a non-blank string"):
        s.keyspace.overlap_cancel_key("  ")


def test_the_send_record_keyspaces():
    import hashlib
    import json

    import pytest

    s = ConversationsSettings()
    # A message ref folds (channel, our identity, participant address, medium id) into ONE opaque
    # sha256 terminal segment: an address or a medium id may carry ``:`` and so is never a raw
    # non-terminal segment.
    ref = s.keyspace.message_ref_key("slack", "T1", "U:2", "1700.1")
    digest = hashlib.sha256(json.dumps(["slack", "T1", "U:2", "1700.1"], separators=(",", ":")).encode())
    assert ref == f"conversations:message_ref:{digest.hexdigest()}"
    assert s.keyspace.message_ref_key("slack", "T1", "U", "2:1700.1") != s.keyspace.message_ref_key(
        "slack", "T1", "U:2", "1700.1"
    )
    assert s.keyspace.message_ref_key_prefix == "conversations:message_ref:"
    assert s.keyspace.ask_send_key("i-1") == "conversations:ask_send:i-1"
    assert s.keyspace.ask_send_key_prefix == "conversations:ask_send:"
    assert s.keyspace.route_sending_key("chat-line") == "conversations:route_sending:chat-line"
    assert s.keyspace.outbound_index_key_prefix == "conversations:outbound:"
    for blank in ("", "  "):
        with pytest.raises(ValueError, match="medium_message_id must be a non-blank string"):
            s.keyspace.message_ref_key("slack", "T1", "U2", blank)
        with pytest.raises(ValueError, match="client_address must be a non-blank string"):
            s.keyspace.message_ref_key("slack", "T1", blank, "1")
        with pytest.raises(ValueError, match="our_identity must be a non-blank string"):
            s.keyspace.message_ref_key("slack", blank, "U2", "1")
        with pytest.raises(ValueError, match="channel must be a non-blank string"):
            s.keyspace.message_ref_key(blank, "T1", "U2", "1")
        with pytest.raises(ValueError, match="interaction_id must be a non-blank string"):
            s.keyspace.ask_send_key(blank)
        with pytest.raises(ValueError, match="route_name must be a non-blank string"):
            s.keyspace.route_sending_key(blank)
    with pytest.raises(ValueError, match="route_name must not contain ':'"):
        s.keyspace.route_sending_key("a:b")
