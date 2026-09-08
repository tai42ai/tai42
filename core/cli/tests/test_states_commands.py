"""The ``tai states`` remote command group, exercised against a fake
``/api/states*`` / ``/api/state-retention*`` server: every command shapes its request
to the door it @covers, the document commands read their body from ``--data`` /
``--file`` / stdin, the paging commands pass their optional query params, and typed
errors surface as a non-zero exit carrying the server message.
"""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli, visible

_RECORD_PATH = "/api/states/status/records/agent/relay/person/alice"


# -- declarations -------------------------------------------------------------


def test_list_gets_the_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/states"
        assert request.headers["x-api-key"] == "test-key"
        return data_response([{"name": "status"}])

    result = run_cli(monkeypatch, handler, ["states", "list"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [{"name": "status"}]


def test_get_reads_one_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/states/status"
        return data_response({"name": "status", "schema": {"type": "object"}})

    result = run_cli(monkeypatch, handler, ["states", "get", "status"])
    assert result.exit_code == 0, result.output
    assert "status" in result.output


def test_put_reads_the_declaration_data(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/states/status"
        assert json.loads(request.content) == {"schema": {"type": "object"}}
        return data_response({"name": "status"})

    result = run_cli(monkeypatch, handler, ["states", "put", "status", "--data", '{"schema": {"type": "object"}}'])
    assert result.exit_code == 0, result.output


def test_put_rejects_both_data_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    doc = tmp_path / "decl.json"
    doc.write_text("{}")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made when both sources are given")

    result = run_cli(monkeypatch, handler, ["states", "put", "status", "--data", "{}", "--file", str(doc)])
    assert result.exit_code != 0
    assert "only one of --data / --file" in visible(result.output)


def test_put_rejects_empty_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made when stdin is empty")

    result = run_cli(monkeypatch, handler, ["states", "put", "status"], stdin="  \n")
    assert result.exit_code != 0
    assert "no document on stdin" in visible(result.output)


def test_delete_hits_the_state_door(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/states/status"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["states", "delete", "status"])
    assert result.exit_code == 0, result.output


def test_stats_reads_the_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/states/status/stats"
        return data_response({"total": 3, "by_kind": {"person": 3}})

    result = run_cli(monkeypatch, handler, ["states", "stats", "status"])
    assert result.exit_code == 0, result.output
    assert "total" in result.output


# -- mounts -------------------------------------------------------------------


def test_mounts_lists_the_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/states/status/mounts"
        return data_response([{"module": "counters", "path": "/c"}])

    result = run_cli(monkeypatch, handler, ["states", "mounts", "status"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [{"module": "counters", "path": "/c"}]


def test_get_mount_reads_one_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/states/status/mounts/counters"
        return data_response({"module": "counters", "path": "/c", "parameters": {}, "declarations": {}})

    result = run_cli(monkeypatch, handler, ["states", "get-mount", "status", "counters"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"module": "counters", "path": "/c", "parameters": {}, "declarations": {}}


def test_mount_puts_the_body_from_a_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    body = tmp_path / "mount.json"
    body.write_text('{"path": "/c", "parameters": {}}')

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/states/status/mounts/counters"
        assert json.loads(request.content) == {"path": "/c", "parameters": {}}
        return data_response({"module": "counters"})

    result = run_cli(monkeypatch, handler, ["states", "mount", "status", "counters", "--file", str(body)])
    assert result.exit_code == 0, result.output


def test_update_mount_patches_the_declarations(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/states/status/mounts/counters"
        assert json.loads(request.content) == {"declarations": {"cap": 10}}
        return data_response({"module": "counters"})

    result = run_cli(
        monkeypatch,
        handler,
        ["states", "update-mount", "status", "counters", "--declarations", '{"cap": 10}'],
    )
    assert result.exit_code == 0, result.output


def test_update_mount_sends_options_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/states/status/mounts/counters"
        assert json.loads(request.content) == {"declarations": {"cap": 10}, "options": {"on_orphan": "close"}}
        return data_response({"module": "counters"})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "update-mount",
            "status",
            "counters",
            "--declarations",
            '{"cap": 10}',
            "--options",
            '{"on_orphan": "close"}',
        ],
    )
    assert result.exit_code == 0, result.output


def test_unmount_deletes_the_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/states/status/mounts/counters"
        return data_response({"unmounted": True})

    result = run_cli(monkeypatch, handler, ["states", "unmount", "status", "counters"])
    assert result.exit_code == 0, result.output


# -- subjects + records -------------------------------------------------------


def test_subjects_pages_with_params(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/states/status/subjects"
        assert request.url.params["kind"] == "person"
        assert request.url.params["limit"] == "2"
        assert request.url.params["cursor"] == "c1"
        return data_response({"items": [], "next_cursor": None})

    result = run_cli(
        monkeypatch,
        handler,
        ["states", "subjects", "status", "--kind", "person", "--limit", "2", "--cursor", "c1"],
    )
    assert result.exit_code == 0, result.output


def test_subjects_omits_absent_params(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.params
        return data_response({"items": [], "next_cursor": None})

    result = run_cli(monkeypatch, handler, ["states", "subjects", "status"])
    assert result.exit_code == 0, result.output


def test_search_posts_filters_with_paging(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/states/status/records/search"
        assert json.loads(request.content) == {"filters": {"tier": "gold"}, "limit": 5, "cursor": "c2"}
        return data_response({"items": [], "next_cursor": None})

    result = run_cli(
        monkeypatch,
        handler,
        ["states", "search", "status", "--filters", '{"tier": "gold"}', "--limit", "5", "--cursor", "c2"],
    )
    assert result.exit_code == 0, result.output


def test_search_omits_absent_paging(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"filters": {}}
        return data_response({"items": []})

    result = run_cli(monkeypatch, handler, ["states", "search", "status", "--filters", "{}"])
    assert result.exit_code == 0, result.output


def test_read_gets_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == _RECORD_PATH
        return data_response({"document": {"count": 1}})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "read",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "person",
            "--key",
            "alice",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "count" in result.output


def test_replace_puts_the_whole_document(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    doc = tmp_path / "doc.json"
    doc.write_text('{"count": 2}')

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == _RECORD_PATH
        assert json.loads(request.content) == {"count": 2}
        return data_response({"document": {"count": 2}})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "replace",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "person",
            "--key",
            "alice",
            "--file",
            str(doc),
        ],
    )
    assert result.exit_code == 0, result.output


def test_merge_patches_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == _RECORD_PATH
        assert json.loads(request.content) == {"count": 3}
        return data_response({"document": {"count": 3}})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "merge",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "person",
            "--key",
            "alice",
        ],
        stdin='{"count": 3}',
    )
    assert result.exit_code == 0, result.output


def test_apply_wraps_a_bare_op_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"{_RECORD_PATH}/deltas"
        assert json.loads(request.content) == {"ops": [{"op": "inc", "path": "/count"}], "op_id": "b1"}
        return data_response({"document": {"count": 4}})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "apply",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "person",
            "--key",
            "alice",
            "--data",
            '[{"op": "inc", "path": "/count"}]',
            "--op-id",
            "b1",
        ],
    )
    assert result.exit_code == 0, result.output


def test_apply_honors_an_envelope_op_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"ops": [{"op": "inc"}], "op_id": "inner"}
        return data_response({"document": {}})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "apply",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "person",
            "--key",
            "alice",
            "--data",
            '{"ops": [{"op": "inc"}], "op_id": "inner"}',
            "--op-id",
            "outer",
        ],
    )
    assert result.exit_code == 0, result.output


def test_erase_deletes_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == _RECORD_PATH
        return data_response({"erased": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "erase",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "person",
            "--key",
            "alice",
        ],
    )
    assert result.exit_code == 0, result.output


def test_fold_posts_into_and_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"{_RECORD_PATH}/fold"
        assert json.loads(request.content) == {
            "into": {"target_kind": "agent", "target_name": "relay", "kind": "person", "key": "bob"},
            "mode": "prefer-target",
        }
        return data_response({"folded": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "fold",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "person",
            "--key",
            "alice",
            "--into",
            '{"target_kind": "agent", "target_name": "relay", "kind": "person", "key": "bob"}',
            "--mode",
            "prefer-target",
        ],
    )
    assert result.exit_code == 0, result.output


def test_writes_pages_with_params(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"{_RECORD_PATH}/writes"
        assert request.url.params["limit"] == "2"
        assert request.url.params["cursor"] == "c3"
        return data_response({"items": [], "next_cursor": None})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "writes",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "person",
            "--key",
            "alice",
            "--limit",
            "2",
            "--cursor",
            "c3",
        ],
    )
    assert result.exit_code == 0, result.output


def test_writes_omits_absent_params(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.params
        return data_response({"items": []})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "writes",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "person",
            "--key",
            "alice",
        ],
    )
    assert result.exit_code == 0, result.output


def test_consumers_lists_the_binders(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/states/status/consumers"
        return data_response({"flows": [], "hooks": [], "schedules": [], "agents": []})

    result = run_cli(monkeypatch, handler, ["states", "consumers", "status"])
    assert result.exit_code == 0, result.output


def test_prune_posts_to_the_retention_door(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/state-retention/prune"
        assert request.content == b""
        return data_response({"pruned": 7})

    result = run_cli(monkeypatch, handler, ["states", "prune"])
    assert result.exit_code == 0, result.output
    assert "pruned" in result.output


# -- record key percent-encoding ----------------------------------------------

# A thread subject's key is the thread id ``bridge:{route}:{quote(principal)}/{id}`` — it
# carries ``/`` and ``:``, both of which must be percent-encoded into ONE path segment so
# the server routes the record door (raw-path matched) rather than splitting the key.
_SLASH_KEY = "bridge:relay:+15550001111/u-42"
_ENCODED_KEY = "bridge%3Arelay%3A%2B15550001111%2Fu-42"


def _read_slash_key_args(key: str) -> list[str]:
    return [
        "states",
        "read",
        "status",
        "--target-kind",
        "agent",
        "--target-name",
        "relay",
        "--kind",
        "thread",
        "--key",
        key,
    ]


def test_record_key_with_slash_is_one_encoded_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # The raw (wire) path keeps the key percent-encoded as a single segment; the
        # decoded path carries the ``/`` back inside the key.
        assert request.url.raw_path.decode() == f"/api/states/status/records/agent/relay/thread/{_ENCODED_KEY}"
        assert request.url.path == f"/api/states/status/records/agent/relay/thread/{_SLASH_KEY}"
        return data_response({"document": {"count": 1}})

    result = run_cli(monkeypatch, handler, _read_slash_key_args(_SLASH_KEY))
    assert result.exit_code == 0, result.output


def test_record_key_ending_in_writes_stays_one_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    # A key whose tail is a sub-action name must not read as ``…/{key}/writes``.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.decode() == "/api/states/status/records/agent/relay/thread/t%2Fwrites"
        return data_response({"document": None})

    result = run_cli(monkeypatch, handler, _read_slash_key_args("t/writes"))
    assert result.exit_code == 0, result.output


def test_record_writes_door_of_a_slashed_key_encodes_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.decode() == (f"/api/states/status/records/agent/relay/thread/{_ENCODED_KEY}/writes")
        return data_response({"entries": [], "next_cursor": None})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "states",
            "writes",
            "status",
            "--target-kind",
            "agent",
            "--target-name",
            "relay",
            "--kind",
            "thread",
            "--key",
            _SLASH_KEY,
        ],
    )
    assert result.exit_code == 0, result.output


# -- error surfacing ----------------------------------------------------------


def test_get_surfaces_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("state 'status' not found", 404)

    result = run_cli(monkeypatch, handler, ["states", "get", "status"])
    assert result.exit_code != 0
    assert "not found" in visible(result.output)
