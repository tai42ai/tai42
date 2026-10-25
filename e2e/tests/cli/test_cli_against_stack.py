"""The ``tai`` CLI command groups against a live booted stack.

~30 command modules are unit-only. This file drives a representative command from
each group against a real stack over subprocess (``tai --server <url>``, auth via
``TAI_API_KEY``), and a MUTATING command for every group whose router mutation is
already e2e-covered (presets, storage, tool_meta, schedules, roles, scopes, keys).

The assertions are OUTPUT CONTRACTS — the machine ``--json`` payload or the emitted
text — never exit codes alone: a command that regressed to a wrong shape (or a
mutation that did not take) fails here even while still exiting 0. This is
representative coverage by design, not a full reference sweep of every subcommand."""

from __future__ import annotations

import base64
import json
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from tai42_e2e.content_samples import SQL, locked_pdf, locked_pdf_message, not_loadable_prefix, png
from tai42_e2e.stack import TaiStack

from ._cli_support import cli_json, run_cli  # pyright: ignore[reportMissingImports]

pytestmark = pytest.mark.needs("cli", "kind:identity", "setting:seeded-access-control")


@pytest.mark.needs("kind:storage", "setting:tool:generate_uuid", "setting:TAI_CONFIG_MODE=file")
def test_reads_across_groups(cli_stack: TaiStack, tmp_path: Path) -> None:
    """At least one READ per group answers with its expected contract."""
    # tools: the loaded toolbox tool is listed.
    tools = run_cli(cli_stack, tmp_path, "tools", "list")
    assert "generate_uuid" in tools.stdout, tools.stdout

    # tools run: the meta-executor door runs a registered tool synchronously and
    # prints its run answer — a ``result`` carrying a version-4 UUID. This exercises the
    # run-tool body shape against a live server, where a wrong shape is a 400 the mock
    # unit tests cannot catch.
    ran = cli_json(run_cli(cli_stack, tmp_path, "tools", "run", "generate_uuid"))
    assert ran["kind"] == "result", ran
    assert uuid.UUID(ran["result"]).version == 4, ran

    # config mode: this stack boots in file config mode.
    mode = run_cli(cli_stack, tmp_path, "config", "mode")
    assert "file" in mode.stdout, mode.stdout

    # manifest show: the manifest's user tools include ask.
    manifest = run_cli(cli_stack, tmp_path, "manifest", "show")
    assert "ask" in manifest.stdout, manifest.stdout

    # system kinds: the pluggable-kind table reports the storage kind.
    kinds = run_cli(cli_stack, tmp_path, "system", "kinds")
    assert "storage" in kinds.stdout, kinds.stdout

    # keys: the seeded root identity is listed (never key material).
    keys = run_cli(cli_stack, tmp_path, "keys", "list")
    assert "e2e-root" in keys.stdout, keys.stdout

    # scopes: the route→scope map carries the seeded e2e-all scope.
    scopes = run_cli(cli_stack, tmp_path, "scopes", "list")
    assert "e2e-all" in scopes.stdout, scopes.stdout

    # roles / presets / schedules / storage: the read answers a well-formed payload of
    # the expected shape — a list of rows for the listings, a mapping for storage info.
    assert isinstance(cli_json(run_cli(cli_stack, tmp_path, "roles", "list")), list)
    assert isinstance(cli_json(run_cli(cli_stack, tmp_path, "presets", "list")), list)
    assert isinstance(cli_json(run_cli(cli_stack, tmp_path, "schedules", "list")), list)
    assert isinstance(cli_json(run_cli(cli_stack, tmp_path, "storage", "info")), dict)


@pytest.mark.needs("setting:tool:generate_uuid")
def test_presets_mutation(cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]) -> None:
    name = uniq("cli-preset")
    created = run_cli(
        cli_stack,
        tmp_path,
        "presets",
        "create",
        name,
        "--base-tool",
        "generate_uuid",
        "--description",
        "CLI-authored preset",
    )
    assert created.returncode == 0, created.stderr
    try:
        listing = cli_json(run_cli(cli_stack, tmp_path, "presets", "list"))
        assert any(isinstance(row, dict) and row.get("name") == name for row in listing), listing
    finally:
        run_cli(cli_stack, tmp_path, "presets", "delete", name)


@pytest.mark.needs("kind:storage", "setting:router:resources")
def test_storage_and_resources_mutation(cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]) -> None:
    resource_id = f"cli/{uniq('note')}.txt"
    body = f"cli-body-{uniq('marker')}"
    uploaded = run_cli(cli_stack, tmp_path, "storage", "upload", resource_id, "--text", body)
    assert uploaded.returncode == 0, uploaded.stderr
    try:
        # storage download returns the raw bytes verbatim.
        downloaded = run_cli(cli_stack, tmp_path, "storage", "download", resource_id, json_flag=False)
        assert body in downloaded.stdout, downloaded.stdout
        # resources get loads the same stored object by id through the resources door.
        fetched = run_cli(cli_stack, tmp_path, "resources", "get", resource_id)
        assert body in fetched.stdout, fetched.stdout
    finally:
        run_cli(cli_stack, tmp_path, "storage", "delete", resource_id)


@pytest.mark.needs("mutable", "setting:tool:generate_uuid")
def test_tool_meta_mutation(cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]) -> None:
    label = f"CLI {uniq('label')}"
    patched = run_cli(cli_stack, tmp_path, "tool-meta", "set", "generate_uuid", "--display-name", label)
    assert patched.returncode == 0, patched.stderr
    listing = run_cli(cli_stack, tmp_path, "tool-meta", "list")
    assert label in listing.stdout, listing.stdout


@pytest.mark.needs("kind:backend", "probe-tools", "setting:extension:schedule_task", "setting:tool:generate_uuid")
def test_schedules_mutation(cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]) -> None:
    schedule_name = uniq("cli-schedule")
    added = run_cli(
        cli_stack,
        tmp_path,
        "schedules",
        "add",
        "run_tool",
        "--tool-kwargs",
        '{"tool_name": "generate_uuid", "arguments": {}}',
        # A once-a-year crontab so it registers without firing during the spec.
        "--cron",
        "0 3 1 1 *",
        "--schedule-name",
        schedule_name,
    )
    assert added.returncode == 0, added.stderr
    try:
        listing = run_cli(cli_stack, tmp_path, "schedules", "list")
        assert schedule_name in listing.stdout, listing.stdout
    finally:
        run_cli(cli_stack, tmp_path, "schedules", "delete", schedule_name)


def test_keys_mutation(cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]) -> None:
    user = uniq("cli-user")
    created = run_cli(
        cli_stack,
        tmp_path,
        "keys",
        "create",
        "--user",
        user,
        "--description",
        "CLI-provisioned key",
        "--scope",
        "e2e-all",
    )
    assert created.returncode == 0, created.stderr
    # The raw sk- key is printed exactly once by create.
    assert "sk-" in created.stdout, created.stdout
    try:
        listing = run_cli(cli_stack, tmp_path, "keys", "list")
        assert user in listing.stdout, listing.stdout
    finally:
        run_cli(cli_stack, tmp_path, "keys", "delete", user)


def test_roles_mutation(cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]) -> None:
    role = uniq("cli-role")
    created = run_cli(cli_stack, tmp_path, "roles", "create", role, "--base-tier", "editor", "--grant", "presets=write")
    assert created.returncode == 0, created.stderr
    try:
        listing = cli_json(run_cli(cli_stack, tmp_path, "roles", "list"))
        assert any(isinstance(row, dict) and row.get("name") == role for row in listing), listing
    finally:
        run_cli(cli_stack, tmp_path, "roles", "delete", role)


def test_scopes_mutation(cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]) -> None:
    url = f"/api/cli-probe/{uniq('scope')}"
    added = run_cli(cli_stack, tmp_path, "scopes", "add", "e2e-all", url)
    assert added.returncode == 0, added.stderr
    try:
        listing = run_cli(cli_stack, tmp_path, "scopes", "list")
        assert url in listing.stdout, listing.stdout
    finally:
        run_cli(cli_stack, tmp_path, "scopes", "remove-url", url)


# The seeded root key's id, the identity a hook registered by the root fires as.
_ROOT_KEY_ID = "e2e-root"


def _cli_upload(stack: TaiStack, cwd: Path, resource_id: str, *, text: str | None = None, data: bytes = b"") -> None:
    content = ["--text", text] if text is not None else ["--base64", base64.b64encode(data).decode()]
    uploaded = run_cli(stack, cwd, "storage", "upload", resource_id, *content)
    assert uploaded.returncode == 0, uploaded.stderr


def _cli_refusal(result: subprocess.CompletedProcess[str], status: int) -> str:
    """The server text a refused command prints after ``Error (HTTP <status>): ``, once it exits 1 with no stdout."""
    head = f"Error (HTTP {status}): "
    assert result.returncode == 1, f"exit {result.returncode}; stdout:\n{result.stdout}"
    assert result.stdout == ""
    assert result.stderr.startswith(head), result.stderr
    return result.stderr.removeprefix(head).rstrip("\n")


@pytest.mark.needs("kind:storage")
def test_the_listings_leave_out_inbound_media_and_list_an_object_beside_it(
    cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]
) -> None:
    media_key = f"inbound-media/{uniq('media')}"
    text_id = f"cli/{uniq('beside')}.txt"
    _cli_upload(cli_stack, tmp_path, media_key, data=png())
    _cli_upload(cli_stack, tmp_path, text_id, text="beside the media")
    try:
        stored = cli_json(run_cli(cli_stack, tmp_path, "storage", "list"))["resources"]
        templates = cli_json(run_cli(cli_stack, tmp_path, "templates", "list"))
        assert text_id in stored
        assert text_id in templates
        assert media_key not in stored
        assert media_key not in templates
    finally:
        run_cli(cli_stack, tmp_path, "storage", "delete", media_key)
        run_cli(cli_stack, tmp_path, "storage", "delete", text_id)


@pytest.mark.needs("kind:storage")
def test_read_and_get_print_a_text_object_and_the_empty_object(
    cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]
) -> None:
    sql_id = f"{uniq('queries')}/lookup.sql"
    empty_id = f"cli/{uniq('empty')}.txt"
    _cli_upload(cli_stack, tmp_path, sql_id, text=SQL)
    _cli_upload(cli_stack, tmp_path, empty_id, text="")
    try:
        for resource_id, content in ((sql_id, SQL), (empty_id, "")):
            read = cli_json(run_cli(cli_stack, tmp_path, "storage", "read", resource_id))
            got = cli_json(run_cli(cli_stack, tmp_path, "templates", "get", resource_id))
            assert (read["kind"], read["content"]) == ("text", content)
            assert (got["kind"], got["content"]) == ("text", content)
    finally:
        run_cli(cli_stack, tmp_path, "storage", "delete", sql_id)
        run_cli(cli_stack, tmp_path, "storage", "delete", empty_id)


@pytest.mark.needs("kind:storage")
def test_read_of_a_locked_pdf_prints_the_refusal_and_exits_1(
    cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]
) -> None:
    resource_id = f"{uniq('docs')}/locked.pdf"
    _cli_upload(cli_stack, tmp_path, resource_id, data=locked_pdf())
    try:
        refusal = _cli_refusal(run_cli(cli_stack, tmp_path, "storage", "read", resource_id, json_flag=False), 400)
        assert refusal == not_loadable_prefix(resource_id) + locked_pdf_message(resource_id)
    finally:
        run_cli(cli_stack, tmp_path, "storage", "delete", resource_id)


@pytest.mark.needs("kind:storage", "setting:tool:generate_uuid")
def test_presets_create_with_a_missing_output_schema_id_prints_the_refusal(
    cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]
) -> None:
    name = uniq("cli-preset")
    missing = f"{uniq('schemas')}/missing"
    created = run_cli(
        cli_stack,
        tmp_path,
        "presets",
        "create",
        name,
        "--base-tool",
        "generate_uuid",
        "--description",
        "CLI-authored preset",
        "--output-schema",
        json.dumps({"id": missing}),
        json_flag=False,
    )
    assert _cli_refusal(created, 400) == f"stored id {missing!r} at output_schema is not a stored template"
    listing = cli_json(run_cli(cli_stack, tmp_path, "presets", "list"))
    assert all(row.get("name") != name for row in listing), listing


@pytest.mark.needs("kind:storage", "setting:tool:generate_uuid")
def test_hooks_register_with_a_missing_condition_id_prints_the_refusal(
    cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]
) -> None:
    missing = f"{uniq('jq')}/gate"
    params = {
        "name": uniq("cli-hook"),
        "topic": uniq("events").replace("_", "-"),
        "tool": "generate_uuid",
        "execution_key": _ROOT_KEY_ID,
        "condition": {"id": missing},
    }
    registered = run_cli(cli_stack, tmp_path, "hooks", "register", "--params", json.dumps(params), json_flag=False)
    line = f"stored id {missing!r} at condition is not a stored template"
    assert _cli_refusal(registered, 400) == f"invalid hook params: {line}"


@pytest.mark.needs("kind:storage", "setting:tool:generate_uuid")
def test_storage_delete_of_a_held_template_prints_the_refusal(
    cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]
) -> None:
    held = f"{uniq('jq')}/gate"
    hook = uniq("cli-hook")
    _cli_upload(cli_stack, tmp_path, held, text=".ok")
    params = {
        "name": hook,
        "topic": uniq("events").replace("_", "-"),
        "tool": "generate_uuid",
        "execution_key": _ROOT_KEY_ID,
        "condition": {"id": held},
    }
    registered = run_cli(cli_stack, tmp_path, "hooks", "register", "--params", json.dumps(params))
    assert registered.returncode == 0, registered.stderr
    try:
        refusal = _cli_refusal(run_cli(cli_stack, tmp_path, "storage", "delete", held, json_flag=False), 409)
        assert refusal == (
            f"stored template {held!r} cannot be deleted — held by:\n"
            f"- hook {hook!r} names {held!r} at condition\n"
            "remove those references first"
        )
        assert cli_json(run_cli(cli_stack, tmp_path, "storage", "read", held))["content"] == ".ok"
    finally:
        run_cli(cli_stack, tmp_path, "hooks", "delete", hook)
    deleted = run_cli(cli_stack, tmp_path, "storage", "delete", held)
    assert deleted.returncode == 0, deleted.stderr
