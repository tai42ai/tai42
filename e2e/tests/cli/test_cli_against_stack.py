"""B7 — the ``tai`` CLI command groups against a live booted stack.

~30 command modules are unit-only. This file drives a representative command from
each group against a real stack over subprocess (``tai --server <url>``, auth via
``TAI_API_KEY``), and a MUTATING command for every group whose router mutation is
already e2e-covered (presets, storage, tool_meta, schedules, roles, scopes, keys).

The assertions are OUTPUT CONTRACTS — the machine ``--json`` payload or the emitted
text — never exit codes alone: a command that regressed to a wrong shape (or a
mutation that did not take) fails here even while still exiting 0. This is
representative coverage by design, not a full reference sweep of every subcommand."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from tai42_e2e.stack import TaiStack

from ._cli_support import cli_json, run_cli  # pyright: ignore[reportMissingImports]


def test_reads_across_groups(cli_stack: TaiStack, tmp_path: Path) -> None:
    """At least one READ per group answers with its expected contract."""
    # tools: the loaded toolbox tool is listed.
    tools = run_cli(cli_stack, tmp_path, "tools", "list")
    assert "generate_uuid" in tools.stdout, tools.stdout

    # tools run: the meta-executor door runs a registered tool synchronously and
    # returns its result — a version-4 UUID. This exercises the run-tool body shape
    # against a live server, where a wrong shape is a 400 the mock unit tests cannot
    # catch.
    ran = cli_json(run_cli(cli_stack, tmp_path, "tools", "run", "generate_uuid"))
    assert uuid.UUID(ran).version == 4, ran

    # config mode: this stack boots in file config mode.
    mode = run_cli(cli_stack, tmp_path, "config", "mode")
    assert "file" in mode.stdout, mode.stdout

    # manifest show: the manifest's user tools include ask_user.
    manifest = run_cli(cli_stack, tmp_path, "manifest", "show")
    assert "ask_user" in manifest.stdout, manifest.stdout

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


def test_tool_meta_mutation(cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]) -> None:
    label = f"CLI {uniq('label')}"
    patched = run_cli(cli_stack, tmp_path, "tool-meta", "set", "generate_uuid", "--display-name", label)
    assert patched.returncode == 0, patched.stderr
    listing = run_cli(cli_stack, tmp_path, "tool-meta", "list")
    assert label in listing.stdout, listing.stdout


def test_schedules_mutation(cli_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]) -> None:
    schedule_name = uniq("cli-schedule")
    added = run_cli(
        cli_stack,
        tmp_path,
        "schedules",
        "add",
        "run_tool_schedule_task",
        "--tool-kwargs",
        '{"tool_name": "generate_uuid", "arguments": {}}',
        # A long cadence so it registers without firing during the spec.
        "--schedule-kwargs",
        f'{{"backend_schedule_name": "{schedule_name}", "backend_schedule": 3600}}',
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
